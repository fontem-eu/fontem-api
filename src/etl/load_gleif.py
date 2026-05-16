"""
GLEIF Full Dump → events.entity_events
=========================================
Downloads (or reads a local copy of) the GLEIF Level 1
concatenated ZIP, streams the XML with iterparse, and emits
``UpsertCompany`` events for each LEI record.

Incremental MERGE-style — no Begin/EndGraphReplace bracket. The
GLEIF dump is multi-million records; bulk-replacing the Company
graph would wipe entries from US listings, sanctions, etc. The
sinks treat each event as MERGE-by-gmr_id, so re-runs are
idempotent.

Usage:
    python -m src.etl.load_gleif
    python -m src.etl.load_gleif --file /tmp/gleif-lei2.zip
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time
import uuid
import zipfile
from xml.etree.ElementTree import iterparse

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog

from . import gmr_id
from ._http import HTTP_HEADERS

logger = logging.getLogger(__name__)

GLEIF_API = "https://leidata.gleif.org/api/v1/concatenated-files/lei2"
NS = "http://www.gleif.org/data/schema/leidata/2016"

# XPath-style tag helpers using the GLEIF namespace
_t = f"{{{NS}}}"
TAG_RECORD = f"{_t}LEIRecord"
TAG_LEI = f"{_t}LEI"
TAG_ENTITY = f"{_t}Entity"
TAG_LEGAL_NAME = f"{_t}LegalName"
TAG_LEGAL_ADDRESS = f"{_t}LegalAddress"
TAG_COUNTRY = f"{_t}Country"
TAG_POSTAL_CODE = f"{_t}PostalCode"
TAG_LEGAL_FORM = f"{_t}LegalForm"
TAG_ENTITY_LEGAL_FORM_CODE = f"{_t}EntityLegalFormCode"
TAG_OTHER_LEGAL_FORM = f"{_t}OtherLegalForm"
TAG_ENTITY_STATUS = f"{_t}EntityStatus"


def resolve_latest_url() -> str:
    """Query the GLEIF API for the latest concatenated file URL."""
    resp = httpx.get(f"{GLEIF_API}?page=0&pageSize=1", timeout=30,
                     headers=HTTP_HEADERS)
    resp.raise_for_status()
    data = resp.json()["data"]
    if not data:
        raise RuntimeError("GLEIF API returned no concatenated files")
    file_id = data[0]["id"]
    return f"{GLEIF_API}/get/{file_id}/zip"


def download_zip(url: str) -> io.BytesIO:
    """Download a ZIP file into memory."""
    logger.info("Downloading %s ...", url)
    buf = io.BytesIO()
    with httpx.stream("GET", url, timeout=600, follow_redirects=True,
                      headers=HTTP_HEADERS) as r:
        r.raise_for_status()
        total = 0
        for chunk in r.iter_bytes(chunk_size=1024 * 256):
            buf.write(chunk)
            total += len(chunk)
            if total % (50 * 1024 * 1024) < 1024 * 256:
                logger.info("  downloaded %d MB", total // (1024 * 1024))
    buf.seek(0)
    logger.info("Download complete: %d MB", total // (1024 * 1024))
    return buf


def parse_gleif_xml(xml_stream):
    """Streaming parser for LEI-CDF v3.1 XML.

    Yields dicts with keys: lei, name, country, postal_code, legal_form, active.
    Memory-efficient: clears each element after processing.
    """
    for _event, elem in iterparse(xml_stream, events=("end",)):
        if elem.tag != TAG_RECORD:
            continue

        lei = _text(elem, TAG_LEI)
        entity = elem.find(TAG_ENTITY)
        if entity is None or not lei:
            elem.clear()
            continue

        name = _text(entity, TAG_LEGAL_NAME)
        addr = entity.find(TAG_LEGAL_ADDRESS)
        country = _text(addr, TAG_COUNTRY) if addr is not None else None
        postal_code = _text(addr, TAG_POSTAL_CODE) if addr is not None else None
        status = _text(entity, TAG_ENTITY_STATUS)

        legal_form_el = entity.find(TAG_LEGAL_FORM)
        legal_form = None
        if legal_form_el is not None:
            legal_form = (
                _text(legal_form_el, TAG_ENTITY_LEGAL_FORM_CODE)
                or _text(legal_form_el, TAG_OTHER_LEGAL_FORM)
            )

        elem.clear()

        if not lei or len(lei) != 20:
            continue

        yield {
            "lei": lei,
            "name": name or None,
            "country": country or None,
            "postal_code": postal_code or None,
            "legal_form": legal_form or None,
            "active": status == "ACTIVE",
        }


def _text(parent, tag):
    """Get text content of a child element, or None."""
    child = parent.find(tag)
    return child.text.strip() if child is not None and child.text else None


def emit_gleif(log: EventLog, records) -> dict:
    """Emit one ``UpsertCompany`` event per LEI record. MERGE-style
    upsert (no bracket); the GLEIF dump is multi-million rows and a
    PUT-replace would wipe Companies from other sources.

    Returns ``{"total": <events>, "elapsed_s": <seconds>}``."""
    batch_id = uuid.uuid4()
    total = 0
    t0 = time.time()

    with log.batch(batch_id, producer="load_gleif") as emit:
        for rec in records:
            company_gmr_id = str(gmr_id.from_lei(rec["lei"]))
            emit.upsert(
                "UpsertCompany",
                iri=f"http://data.fontem.eu/id/Company/{company_gmr_id}",
                domain="company",
                payload=builders.upsert_company(
                    gmr_id=company_gmr_id,
                    lei=rec["lei"],
                    name=rec.get("name"),
                    country=rec.get("country"),
                    legal_form=rec.get("legal_form"),
                    postal_code=rec.get("postal_code"),
                    active=rec.get("active"),
                ),
            )
            total += 1
            if total % 50000 == 0:
                elapsed = time.time() - t0
                rate = total / elapsed if elapsed else 0
                logger.info("  %d companies emitted (%.0f/s)", total, rate)

    elapsed = time.time() - t0
    logger.info("Done: %d companies in %.1fs", total, elapsed)
    return {"total": total, "elapsed_s": round(elapsed, 1)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertCompany events for the GLEIF dump",
    )
    parser.add_argument(
        "--file", help="Path to a local GLEIF ZIP file",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.file:
        logger.info("Reading local file: %s", args.file)
        zf = zipfile.ZipFile(args.file)
    else:
        url = resolve_latest_url()
        buf = download_zip(url)
        zf = zipfile.ZipFile(buf)

    xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
    if not xml_names:
        logger.error("No XML file found in ZIP")
        sys.exit(1)

    xml_name = xml_names[0]
    logger.info("Parsing %s ...", xml_name)

    log = EventLog.from_env()
    try:
        with zf.open(xml_name) as xml_stream:
            records = parse_gleif_xml(xml_stream)
            emit_gleif(log, records)
    finally:
        log.close()


if __name__ == "__main__":
    main()
