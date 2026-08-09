"""
ESMA FIRDS (Financial Instruments Reference Data) → event log
==============================================================
Downloads delta files from the ESMA FIRDS Solr register, extracts
XML from ZIPs, and emits ``UpsertListing`` events keyed by ISIN.
Only equity and collective-investment instruments (CFI starting
with 'E' or 'C') are kept.

Listings are keyed by ticker in the schema, but FIRDS only carries
ISINs — there is no ticker attached to an instrument until OpenFIGI
or another mapping source confirms one. We use the ISIN as the
ticker primary key. OpenFIGI subsequently emits a separate
UpsertListing event with the canonical ticker; the consolidator
links the two via AssertSameAs.

Issuer linkage (Listing → parent Company) goes through the
``Issr`` LEI in the FIRDS XML. Companies are matched by LEI in
the derived Neo4j store (the canonical write path is
load_gleif which already runs before us).

Usage:
    python -m src.etl.load_firds --since 2025-09-01
    python -m src.etl.load_firds --file /tmp/firds_delta.zip
"""

from __future__ import annotations


import argparse
import io
import logging
import os
import sys
import time
import uuid
import zipfile
from collections.abc import Iterable
from xml.etree.ElementTree import iterparse

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

from src.etl._http_retry import RateLimiter, get_with_retry
from src.etl.data_description import DataDescription

DESCRIPTION = DataDescription(
    producer="load_firds",
    label="FIRDS Instruments",
    theme="securities",
    summary="Reference data for financial instruments traded in the EU.",
    entities=(
        "Listing",
    ),
    coverage="Instruments admitted to trading on EU venues.",
    upstream="ESMA FIRDS",
    update_freq="daily",
    answers=(
        "Which venue an instrument trades on, and under which ISIN",
    ),
)


logger = logging.getLogger(__name__)

FIRDS_SOLR = (
    "https://registers.esma.europa.eu/solr/"
    "esma_registers_firds_files/select"
)
EMIT_BATCH = 1000

# ESMA Solr + the FIRDS delta-zip CDN both sit behind an Azure
# Application Gateway that silently drops TLS handshakes after a
# small burst — we've watched it succeed at minute T+0 and fail
# repeatedly with "TLS handshake timed out" at T+1 from the same
# egress IP. The WAF's window seems to be 60 s. 6 requests/minute
# stays well clear of whatever triggers the block, and the per-zip
# downloads are 1-2 MB each so the throughput hit is marginal
# (300-500 zips of an 8-month delta ≈ 60 min vs ~5 min unthrottled).
# Adjustable via FIRDS_RATE_LIMIT_RPM if a future run needs to be
# even more conservative.
_FIRDS_RATE_LIMIT_RPM = float(os.environ.get("FIRDS_RATE_LIMIT_RPM", "6"))

# Longer retry budget than the loader default (3 attempts / 5 s base)
# because ESMA's WAF can stay angry for tens of seconds at a time. A
# single ConnectTimeout is almost always followed by another one if
# you retry within 30 s; we want 5+ attempts spread across several
# minutes before declaring the upstream genuinely down.
#   attempt 1: instant (rate limiter)
#   attempt 2: up to 15 s extra
#   attempt 3: up to 30 s
#   attempt 4: up to 60 s
#   attempt 5: up to 120 s
#   attempt 6: up to 240 s
# Worst-case wall clock between attempt 1 and attempt 6 ≈ 8 min.
_FIRDS_MAX_ATTEMPTS = 6
_FIRDS_BASE_DELAY_S = 15.0

_firds_limiter = RateLimiter.per_minute(_FIRDS_RATE_LIMIT_RPM)

# Resolve LEI → gmr_id from the derived Neo4j store. Companies must
# have been projected by load_gleif (or another LEI-aware loader)
# before FIRDS runs — the ETL CronJob schedule reflects that.
LEI_TO_GMR = """
UNWIND $leis AS lei
MATCH (c:Company) WHERE c.lei = lei
RETURN c.lei AS lei, c.gmr_id AS gmr_id
"""


def query_firds_files(since):
    """Query FIRDS Solr for delta file URLs since a given date."""
    params = {
        "q": "*",
        "fq": [
            f"publication_date:[{since}T00:00:00Z TO *]",
            "file_type:DLTINS",
        ],
        "wt": "json",
        "rows": 200,
        "sort": "publication_date desc",
    }
    logger.info("Querying FIRDS Solr for deltas since %s ...", since)
    try:
        resp = get_with_retry(
            FIRDS_SOLR, params=params, timeout=60,
            max_attempts=_FIRDS_MAX_ATTEMPTS,
            base_delay=_FIRDS_BASE_DELAY_S,
            rate_limiter=_firds_limiter,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to query FIRDS Solr")
        sys.exit(1)

    docs = resp.json().get("response", {}).get("docs", [])
    urls = [d["download_link"] for d in docs if "download_link" in d]
    logger.info("Found %d delta files", len(urls))
    return urls


# Disk cache for downloaded zips. DLTINS files are immutable — once ESMA
# publishes (date, part), the file's contents never change — so a plain
# filename-keyed cache with no TTL is correct. The default empty string
# disables caching (handy for unit tests + the --file mode); the
# CronJob sets FIRDS_CACHE_DIR to the NFS-backed PVC mount in deployed
# environments. Atomic writes via .partial → rename keep a half-written
# zip from being treated as cached on a crash.
_FIRDS_CACHE_DIR = os.environ.get("FIRDS_CACHE_DIR", "")


def _cache_path_for(url):
    """Map a FIRDS zip URL to its cache file path. Returns None when
    caching is disabled."""
    if not _FIRDS_CACHE_DIR:
        return None
    name = url.rsplit("/", 1)[-1]
    # Belt + braces: drop anything that wouldn't be a valid filename so
    # an unexpected URL shape can't escape the cache dir.
    if "/" in name or "\\" in name or not name:
        return None
    return os.path.join(_FIRDS_CACHE_DIR, name)


def download_zip(url):
    """Download a ZIP file into an in-memory buffer.

    When ``FIRDS_CACHE_DIR`` is set and the URL's filename already
    exists in that dir, the bytes are read from disk instead of from
    ESMA — DLTINS files are immutable, so a hit is always safe. On a
    miss, the downloaded bytes are written atomically to the cache
    (``.partial`` → rename) before being returned so a crash mid-write
    can't leave a half-zip masquerading as cached.
    """
    cache_path = _cache_path_for(url)
    if cache_path and os.path.exists(cache_path):
        logger.info("Cache hit: %s", cache_path)
        with open(cache_path, "rb") as fh:
            return io.BytesIO(fh.read())

    logger.info("Downloading %s ...", url)
    try:
        resp = get_with_retry(
            url, timeout=300, follow_redirects=True,
            max_attempts=_FIRDS_MAX_ATTEMPTS,
            base_delay=_FIRDS_BASE_DELAY_S,
            rate_limiter=_firds_limiter,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to download %s", url)
        return None

    if cache_path:
        os.makedirs(_FIRDS_CACHE_DIR, exist_ok=True)
        partial = cache_path + ".partial"
        with open(partial, "wb") as fh:
            fh.write(resp.content)
        os.replace(partial, cache_path)
        logger.info("Cached → %s", cache_path)
    return io.BytesIO(resp.content)


# DLTINS = Daily Listed Trading Instruments New/Modified/Terminated. Each
# instrument lives under exactly one of these wrappers — never a generic
# <RefData> element (that's a different ESMA schema; auth.036.001.03 uses
# the three-wrapper form). Discovered the hard way: the loader looked for
# <RefData> for months, parsed zero records, and "successfully" emitted
# zero events on every run.
_RECORD_WRAPPERS = {"NewRcrd", "ModfdRcrd", "TermntdRcrd"}


def parse_firds_xml(xml_stream, stats=None):
    """Stream-parse FIRDS DLTINS XML and yield instrument dicts.
    Keeps only equities (CFI starts with 'E') and collective
    investment schemes (CFI starts with 'C').

    ``stats`` (dict, optional) is mutated to record:
      * ``fin_instrm_seen`` — count of every <FinInstrm> element in
        the document, regardless of wrapper or CFI filter. Always
        non-zero for a valid DLTINS file; if a run sees zero across
        all zips, the file format or our entry-point assumption
        changed and the caller should fail loud.
      * ``records_yielded`` — count of records that survived the
        wrapper + CFI filter and got yielded.
    """
    fin_instrm = 0
    yielded = 0
    for _event, elem in iterparse(xml_stream, events=("end",)):
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "FinInstrm":
            fin_instrm += 1
            # Clear so the parent doesn't accumulate half a million
            # children on a real zip (each FinInstrm is small after
            # the wrapper clear, but 500k of them still bloats memory).
            elem.clear()
            continue
        if tag not in _RECORD_WRAPPERS:
            continue
        gnl = None
        for child in elem:
            child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if child_tag == "FinInstrmGnlAttrbts":
                gnl = child
                break
        if gnl is None:
            elem.clear()
            continue
        record = _extract_instrument(gnl, elem, wrapper_tag=tag)
        elem.clear()
        if record:
            yielded += 1
            yield record
    if stats is not None:
        stats["fin_instrm_seen"] = stats.get("fin_instrm_seen", 0) + fin_instrm
        stats["records_yielded"] = stats.get("records_yielded", 0) + yielded


def _extract_instrument(gnl_elem, record_elem, wrapper_tag="ModfdRcrd"):
    """Extract instrument data from FIRDS XML elements.

    ``wrapper_tag`` is the surrounding NewRcrd/ModfdRcrd/TermntdRcrd
    element name, used to set ``active`` (False for terminations).
    """
    isin = _child_text(gnl_elem, "Id")
    name = _child_text(gnl_elem, "FullNm")
    cfi = _child_text(gnl_elem, "ClssfctnTp")

    if not isin or len(isin) != 12:
        return None
    if not cfi or (not cfi.startswith("E") and not cfi.startswith("C")):
        return None

    currency = _child_text(gnl_elem, "NtnlCcy")

    mic = ""
    lei = ""
    for child in record_elem:
        child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if child_tag == "TradgVnRltdAttrbts":
            mic = _child_text(child, "Id")
        elif child_tag == "Issr":
            lei = (child.text or "").strip()

    instrument_type = "equity" if cfi.startswith("E") else "fund"

    return {
        "isin": isin,
        "instrument_name": (name or "")[:200],
        "instrument_type": instrument_type,
        "cfi_code": cfi,
        "trading_venue_mic": mic,
        "currency": currency or "",
        "lei": lei if lei and len(lei) == 20 else None,
        "active": wrapper_tag != "TermntdRcrd",
    }


def _child_text(elem, tag_suffix):
    """Get text of a child element by local tag name."""
    for child in elem:
        child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if child_tag == tag_suffix:
            return (child.text or "").strip()
    return ""


def resolve_leis(driver, records: list[dict]) -> dict[str, str]:
    """Look up gmr_id for each record's LEI. Returns {lei: gmr_id}.
    Records whose LEI doesn't resolve will be emitted without a
    company_gmr_id — the schema allows None for now (those orphan
    Listings are the price of incremental migration; the
    consolidator can later re-link them when the parent Company
    lands)."""
    leis = list({r["lei"] for r in records if r.get("lei")})
    if not leis:
        return {}
    resolved: dict[str, str] = {}
    with driver.session() as session:
        for row in session.run(LEI_TO_GMR, leis=leis):
            resolved[row["lei"]] = row["gmr_id"]
    return resolved


def emit_listings(
    log: EventLog, records: Iterable[dict], lei_to_gmr: dict[str, str],
) -> dict:
    """Emit UpsertListing events for the given records. Records
    without a resolvable LEI → gmr_id mapping are skipped (we need
    a parent Company to anchor the Listing). Returns counters.

    To keep the per-batch transaction bounded we emit in EMIT_BATCH
    chunks; each chunk is its own batch_id."""
    total = 0
    emitted = 0
    skipped = 0
    chunk: list[dict] = []

    def _flush(buf: list[dict]) -> int:
        if not buf:
            return 0
        batch_id = uuid.uuid4()
        n = 0
        with log.batch(batch_id, producer="load_firds") as emit:
            for rec in buf:
                emit.upsert(
                    "UpsertListing",
                    iri=f"http://data.fontem.eu/id/Listing/{rec['isin']}",
                    domain="listing",
                    payload=builders.upsert_listing(
                        # FIRDS has no ticker — ISIN is the stable
                        # identifier. OpenFIGI later emits the
                        # canonical ticker as a separate event.
                        ticker=rec["isin"],
                        company_gmr_id=rec["company_gmr_id"],
                        currency=rec["currency"] or None,
                        isin=rec["isin"],
                        mic=rec["trading_venue_mic"] or None,
                        active=rec.get("active", True),
                    ),
                )
                n += 1
        return n

    for rec in records:
        total += 1
        gmr_id = lei_to_gmr.get(rec.get("lei") or "")
        if not gmr_id:
            skipped += 1
            continue
        rec["company_gmr_id"] = gmr_id
        chunk.append(rec)
        if len(chunk) >= EMIT_BATCH:
            emitted += _flush(chunk)
            chunk = []

    emitted += _flush(chunk)
    return {"total": total, "emitted": emitted, "skipped": skipped}


class FirdsParseError(RuntimeError):
    """Raised when a FIRDS run completed but the XML parser surfaced
    no records from non-empty input. This is the "silent failure"
    signal that the wrapper-tag mismatch bug used to produce; raising
    instead of warning ensures a future schema drift fails the
    cronjob rather than emitting zero events for months."""


def _new_summary() -> dict:
    return {
        "total": 0,
        "emitted": 0,
        "skipped": 0,
        "files_processed": 0,
        "fin_instrm_seen": 0,
        "records_yielded": 0,
    }


def _assert_parser_made_progress(summary: dict) -> None:
    """Fail loud if zips were processed but zero records came out of
    the parser. Legitimate cases that don't trigger this:
      * zero zips downloaded (Solr returned empty / all downloads failed)
        — handled separately by the URL list being empty
      * zips processed, records yielded, but all skipped because no
        Issr LEI resolves to a Company in Neo4j — that's a data-overlap
        outcome, not a parser bug
    """
    if (
        summary["files_processed"] > 0
        and summary["fin_instrm_seen"] > 0
        and summary["records_yielded"] == 0
    ):
        raise FirdsParseError(
            f"FIRDS run processed {summary['files_processed']} zip(s) "
            f"containing {summary['fin_instrm_seen']} <FinInstrm> elements "
            "but the parser yielded zero records. This is the wrapper-tag /"
            " CFI-filter mismatch signature — ESMA changed the schema or "
            "the filter is dropping everything."
        )


def _load_from_file(driver, log, file_path) -> dict:
    """Parse a local ZIP and emit all instruments."""
    logger.info("Reading local file: %s", file_path)
    summary = _new_summary()
    try:
        with zipfile.ZipFile(file_path) as zf:
            xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
            if not xml_names:
                logger.error("No XML file found in ZIP")
                sys.exit(1)
            all_records: list[dict] = []
            parse_stats: dict = {}
            for xml_name in xml_names:
                with zf.open(xml_name) as xml_stream:
                    all_records.extend(parse_firds_xml(xml_stream, stats=parse_stats))
            summary["files_processed"] = 1
            summary["fin_instrm_seen"] = parse_stats.get("fin_instrm_seen", 0)
            summary["records_yielded"] = parse_stats.get("records_yielded", 0)
    except (OSError, zipfile.BadZipFile):
        logger.exception("Failed to open ZIP %s", file_path)
        sys.exit(1)
    if all_records:
        lei_to_gmr = resolve_leis(driver, all_records)
        emit = emit_listings(log, all_records, lei_to_gmr)
        for k in ("total", "emitted", "skipped"):
            summary[k] += emit[k]
    _assert_parser_made_progress(summary)
    return summary


def _load_from_solr(driver, log, since) -> dict:  # pylint: disable=too-many-locals
    """Download delta ZIPs from FIRDS Solr and emit instruments."""
    urls = query_firds_files(since)
    summary = _new_summary()
    for url in urls:
        buf = download_zip(url)
        if buf is None:
            continue
        try:
            zf = zipfile.ZipFile(buf)  # pylint: disable=consider-using-with
        except zipfile.BadZipFile:
            logger.warning("Skipping bad ZIP: %s", url)
            continue
        summary["files_processed"] += 1
        xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
        for xml_name in xml_names:
            parse_stats: dict = {}
            with zf.open(xml_name) as xml_stream:
                records = list(parse_firds_xml(xml_stream, stats=parse_stats))
            summary["fin_instrm_seen"] += parse_stats.get("fin_instrm_seen", 0)
            summary["records_yielded"] += parse_stats.get("records_yielded", 0)
            if not records:
                continue
            lei_to_gmr = resolve_leis(driver, records)
            part = emit_listings(log, records, lei_to_gmr)
            for k in ("total", "emitted", "skipped"):
                summary[k] += part[k]
    _assert_parser_made_progress(summary)
    return summary


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit FIRDS instrument events into the event log",
    )
    parser.add_argument("--file", help="Path to a local FIRDS ZIP file")
    parser.add_argument(
        "--since", default="2025-09-01",
        help="Download deltas since this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--neo4j-uri",
        default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
    )
    parser.add_argument(
        "--neo4j-user",
        default=os.environ.get("NEO4J_USER", "neo4j"),
    )
    parser.add_argument(
        "--neo4j-password",
        default=os.environ.get("NEO4J_PASSWORD", ""),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password),
    )
    log = EventLog.from_env()
    t0 = time.time()
    try:
        if args.file:
            summary = _load_from_file(driver, log, args.file)
        else:
            summary = _load_from_solr(driver, log, args.since)
    finally:
        driver.close()
        log.close()
    elapsed = time.time() - t0
    logger.info(
        "FIRDS: %d zips, %d <FinInstrm> seen, %d records yielded, "
        "%d events emitted, %d skipped (no LEI match) in %.1fs",
        summary["files_processed"], summary["fin_instrm_seen"],
        summary["records_yielded"],
        summary["emitted"], summary["skipped"], elapsed,
    )


if __name__ == "__main__":
    main()
