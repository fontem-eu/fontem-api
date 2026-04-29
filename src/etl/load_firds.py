"""
ESMA FIRDS (Financial Instruments Reference Data) → Neo4j
=========================================================
Downloads delta files from the ESMA FIRDS Solr register, extracts XML
from ZIPs, and MERGEs Listing nodes into Neo4j.  Only equity and
collective-investment instruments (CFI starting with 'E' or 'C') are
kept.

Usage:
    python -m src.etl.load_firds --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_firds --file /tmp/firds_delta.zip
    python -m src.etl.load_firds --since 2025-10-01
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
import zipfile
from xml.etree.ElementTree import iterparse

import httpx
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

FIRDS_SOLR = (
    "https://registers.esma.europa.eu/solr/"
    "esma_registers_firds_files/select"
)
BATCH_SIZE = 2000

CONSTRAINT_CYPHER = """
CREATE CONSTRAINT listing_isin IF NOT EXISTS
FOR (l:Listing) REQUIRE l.isin IS UNIQUE
"""

MERGE_LISTING = """
UNWIND $batch AS row
MERGE (l:Listing {isin: row.isin})
SET l.instrument_name    = row.instrument_name,
    l.instrument_type    = row.instrument_type,
    l.cfi_code           = row.cfi_code,
    l.trading_venue_mic  = row.trading_venue_mic,
    l.currency           = row.currency,
    l.lei                = row.lei
"""

LINK_COMPANY = """
UNWIND $batch AS row
MATCH (l:Listing {isin: row.isin})
WHERE row.lei IS NOT NULL AND size(row.lei) = 20
WITH l, row
MATCH (c:Company {lei: row.lei})
MERGE (c)-[:LISTED_AS]->(l)
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
    """
    Stream-parse FIRDS DLTINS XML and yield instrument dicts.

    Keeps only equities (CFI starts with 'E') and collective investment
    schemes (CFI starts with 'C').
    """
    for _event, elem in iterparse(xml_stream, events=("end",)):
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag not in ("FinInstrmGnlAttrbts", "RefData"):
            elem.clear()
            continue

        if tag == "RefData":
            # Try to extract from full RefData element
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
            continue

        elem.clear()


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

    # Trading venue from parent
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


def load_into_neo4j(driver, records, batch_size=BATCH_SIZE):
    """MERGE Listing nodes and link to Company via LEI."""
    total = 0
    linked = 0
    batch = []
    t0 = time.time()

    with driver.session() as session:
        session.run(CONSTRAINT_CYPHER)
        logger.info("Constraint ensured")

        for rec in records:
            batch.append(rec)
            if len(batch) >= batch_size:
                session.run(MERGE_LISTING, batch=batch)
                result = session.run(LINK_COMPANY, batch=batch)
                linked += result.consume().counters.relationships_created
                total += len(batch)
                batch = []
                if total % 50000 < batch_size:
                    elapsed = time.time() - t0
                    rate = total / elapsed if elapsed else 0
                    logger.info(
                        "  %d listings loaded (%.0f/s), %d linked",
                        total, rate, linked,
                    )

        if batch:
            session.run(MERGE_LISTING, batch=batch)
            result = session.run(LINK_COMPANY, batch=batch)
            linked += result.consume().counters.relationships_created
            total += len(batch)

    elapsed = time.time() - t0
    return {"total": total, "linked": linked, "elapsed_s": round(elapsed, 1)}


def _load_from_file(driver, file_path):
    """Parse a local ZIP and load all instruments."""
    logger.info("Reading local file: %s", file_path)
    try:
        with zipfile.ZipFile(file_path) as zf:
            xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
            if not xml_names:
                logger.error("No XML file found in ZIP")
                sys.exit(1)
            all_records = []
            for xml_name in xml_names:
                with zf.open(xml_name) as xml_stream:
                    all_records.extend(parse_firds_xml(xml_stream))
    except (OSError, zipfile.BadZipFile):
        logger.exception("Failed to open ZIP %s", file_path)
        sys.exit(1)
    return load_into_neo4j(driver, all_records)


def _load_from_solr(driver, since):
    """Download delta ZIPs from FIRDS Solr and load instruments."""
    urls = query_firds_files(since)
    summary = {"total": 0, "linked": 0, "elapsed_s": 0}
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
            if records:
                part = load_into_neo4j(driver, records)
                summary["total"] += part["total"]
                summary["linked"] += part["linked"]
                summary["elapsed_s"] += part["elapsed_s"]
    return summary


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load ESMA FIRDS instrument data into Neo4j"
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
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )

    try:
        if args.file:
            summary = _load_from_file(driver, args.file)
        else:
            summary = _load_from_solr(driver, args.since)
        # Coverage range is what's in the graph (cumulative across runs).
        with driver.session() as session:
            total_listings = session.run(
                "MATCH (l:Listing) RETURN count(l) AS n"
            ).single()["n"]
        from src.etl import _freshness  # pylint: disable=import-outside-toplevel
        _freshness.update_source(
            driver,
            source_id="firds",
            label="ESMA FIRDS instrument reference data",
            coverage_start=None,
            coverage_end=None,
            record_count=int(total_listings),
            expected_cadence_hours=25,  # daily
        )
    finally:
        driver.close()

    logger.info(
        "Done: %d listings, %d linked to companies in %.1fs",
        summary["total"],
        summary["linked"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
