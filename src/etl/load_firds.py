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
from gmr_event_schemas import builders
from gmr_events import EventLog
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

FIRDS_SOLR = (
    "https://registers.esma.europa.eu/solr/"
    "esma_registers_firds_files/select"
)
EMIT_BATCH = 1000

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
        resp = httpx.get(FIRDS_SOLR, params=params, timeout=60)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to query FIRDS Solr")
        sys.exit(1)

    docs = resp.json().get("response", {}).get("docs", [])
    urls = [d["download_link"] for d in docs if "download_link" in d]
    logger.info("Found %d delta files", len(urls))
    return urls


def download_zip(url):
    """Download a ZIP file into an in-memory buffer."""
    logger.info("Downloading %s ...", url)
    try:
        resp = httpx.get(url, timeout=300, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to download %s", url)
        return None
    return io.BytesIO(resp.content)


def parse_firds_xml(xml_stream):
    """Stream-parse FIRDS DLTINS XML and yield instrument dicts.
    Keeps only equities (CFI starts with 'E') and collective
    investment schemes (CFI starts with 'C')."""
    for _event, elem in iterparse(xml_stream, events=("end",)):
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag != "RefData":
            elem.clear()
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
        record = _extract_instrument(gnl, elem)
        elem.clear()
        if record:
            yield record


def _extract_instrument(gnl_elem, ref_data_elem):
    """Extract instrument data from FIRDS XML elements."""
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
    for child in ref_data_elem:
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
                        active=True,
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


def _load_from_file(driver, log, file_path) -> dict:
    """Parse a local ZIP and emit all instruments."""
    logger.info("Reading local file: %s", file_path)
    try:
        with zipfile.ZipFile(file_path) as zf:
            xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
            if not xml_names:
                logger.error("No XML file found in ZIP")
                sys.exit(1)
            all_records: list[dict] = []
            for xml_name in xml_names:
                with zf.open(xml_name) as xml_stream:
                    all_records.extend(parse_firds_xml(xml_stream))
    except (OSError, zipfile.BadZipFile):
        logger.exception("Failed to open ZIP %s", file_path)
        sys.exit(1)
    if not all_records:
        return {"total": 0, "emitted": 0, "skipped": 0}
    lei_to_gmr = resolve_leis(driver, all_records)
    return emit_listings(log, all_records, lei_to_gmr)


def _load_from_solr(driver, log, since) -> dict:
    """Download delta ZIPs from FIRDS Solr and emit instruments."""
    urls = query_firds_files(since)
    summary = {"total": 0, "emitted": 0, "skipped": 0}
    for url in urls:
        buf = download_zip(url)
        if buf is None:
            continue
        try:
            zf = zipfile.ZipFile(buf)  # pylint: disable=consider-using-with
        except zipfile.BadZipFile:
            logger.warning("Skipping bad ZIP: %s", url)
            continue
        xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
        for xml_name in xml_names:
            with zf.open(xml_name) as xml_stream:
                records = list(parse_firds_xml(xml_stream))
            if not records:
                continue
            lei_to_gmr = resolve_leis(driver, records)
            part = emit_listings(log, records, lei_to_gmr)
            for k in summary:
                summary[k] += part[k]
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
        "FIRDS: %d instruments, %d events emitted, %d skipped (no LEI match) "
        "in %.1fs",
        summary["total"], summary["emitted"], summary["skipped"], elapsed,
    )


if __name__ == "__main__":
    main()
