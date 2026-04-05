"""
GLEIF Full Dump → Neo4j Company Loader
=======================================
Downloads (or reads a local copy of) the GLEIF Level 1 concatenated
ZIP, streams the XML with iterparse, and MERGEs Company nodes into
Neo4j.

Usage:
    python -m src.etl.load_gleif --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_gleif --file /tmp/gleif-lei2.zip
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time
import zipfile
from xml.etree.ElementTree import iterparse

import httpx
from neo4j import GraphDatabase

from . import gmr_id

logger = logging.getLogger(__name__)

GLEIF_API = "https://leidata.gleif.org/api/v1/concatenated-files/lei2"
NS = "http://www.gleif.org/data/schema/leidata/2016"
BATCH_SIZE = 2000

# XPath-style tag helpers using the GLEIF namespace
_t = f"{{{NS}}}"
TAG_RECORD = f"{_t}LEIRecord"
TAG_LEI = f"{_t}LEI"
TAG_ENTITY = f"{_t}Entity"
TAG_LEGAL_NAME = f"{_t}LegalName"
TAG_LEGAL_ADDRESS = f"{_t}LegalAddress"
TAG_COUNTRY = f"{_t}Country"
TAG_LEGAL_FORM = f"{_t}LegalForm"
TAG_ENTITY_LEGAL_FORM_CODE = f"{_t}EntityLegalFormCode"
TAG_OTHER_LEGAL_FORM = f"{_t}OtherLegalForm"
TAG_ENTITY_STATUS = f"{_t}EntityStatus"


def resolve_latest_url() -> str:
    """Query the GLEIF API for the latest concatenated file URL."""
    resp = httpx.get(f"{GLEIF_API}?page=0&pageSize=1", timeout=30)
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
    with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
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
    """
    Streaming parser for LEI-CDF v3.1 XML.

    Yields dicts with keys: lei, name, country, legal_form, active.
    Memory-efficient: clears each element after processing.
    """
    for event, elem in iterparse(xml_stream, events=("end",)):
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
            "name": name or "",
            "country": country or "",
            "legal_form": legal_form or "",
            "active": status == "ACTIVE",
        }


def _text(parent, tag):
    """Get text content of a child element, or None."""
    child = parent.find(tag)
    return child.text.strip() if child is not None and child.text else None


def load_into_neo4j(driver, records, batch_size=BATCH_SIZE):
    """
    MERGE Company nodes into Neo4j in batches.

    Returns a summary dict with counts.
    """
    query = """
    UNWIND $batch AS row
    MERGE (c:Company {gmr_id: row.gmr_id})
    SET c.lei        = row.lei,
        c.name       = row.name,
        c.country    = row.country,
        c.legal_form = row.legal_form,
        c.active     = row.active
    """

    total = 0
    batch = []
    t0 = time.time()

    with driver.session() as session:
        # Create constraint (idempotent)
        session.run(
            "CREATE CONSTRAINT company_gmr_id IF NOT EXISTS "
            "FOR (c:Company) REQUIRE c.gmr_id IS UNIQUE"
        )

        for rec in records:
            rec["gmr_id"] = gmr_id.from_lei(rec["lei"])
            batch.append(rec)

            if len(batch) >= batch_size:
                session.run(query, batch=batch)
                total += len(batch)
                batch = []
                if total % 50000 < batch_size:
                    elapsed = time.time() - t0
                    rate = total / elapsed if elapsed else 0
                    logger.info(
                        "  %d companies loaded (%.0f/s)", total, rate
                    )

        if batch:
            session.run(query, batch=batch)
            total += len(batch)

    elapsed = time.time() - t0
    return {"total": total, "elapsed_s": round(elapsed, 1)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load GLEIF LEI data into Neo4j"
    )
    parser.add_argument(
        "--file", help="Path to a local GLEIF ZIP file"
    )
    parser.add_argument(
        "--neo4j-uri",
        default="bolt://neo4j:7687",
        help="Neo4j bolt URI (default: bolt://neo4j:7687)",
    )
    parser.add_argument(
        "--neo4j-user", default="neo4j",
        help="Neo4j username",
    )
    parser.add_argument(
        "--neo4j-password", default="gmr-neo4j-2026",
        help="Neo4j password",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Open the ZIP
    if args.file:
        logger.info("Reading local file: %s", args.file)
        zf = zipfile.ZipFile(args.file)
    else:
        url = resolve_latest_url()
        buf = download_zip(url)
        zf = zipfile.ZipFile(buf)

    # Find the XML inside the ZIP
    xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
    if not xml_names:
        logger.error("No XML file found in ZIP")
        sys.exit(1)

    xml_name = xml_names[0]
    logger.info("Parsing %s ...", xml_name)

    with zf.open(xml_name) as xml_stream:
        records = parse_gleif_xml(xml_stream)

        driver = GraphDatabase.driver(
            args.neo4j_uri,
            auth=(args.neo4j_user, args.neo4j_password),
        )
        try:
            summary = load_into_neo4j(driver, records)
        finally:
            driver.close()

    logger.info(
        "Done: %d companies in %.1fs",
        summary["total"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
