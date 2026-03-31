"""
GLEIF Level 2 (Relationships) → Neo4j [:SUBSIDIARY_OF]
=======================================================
Downloads the GLEIF relationship records (parent-subsidiary) and
creates [:SUBSIDIARY_OF] relationships between Company nodes.

Usage:
    python -m src.etl.load_gleif_relationships --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_gleif_relationships --file /tmp/gleif-rr.zip
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

GLEIF_RR_API = "https://leidata.gleif.org/api/v1/concatenated-files/rr"
NS = "http://www.gleif.org/data/schema/rr/2016"
BATCH_SIZE = 2000

_t = f"{{{NS}}}"
TAG_RECORD = f"{_t}RelationshipRecord"
TAG_RELATIONSHIP = f"{_t}Relationship"
TAG_START_NODE = f"{_t}StartNode"
TAG_END_NODE = f"{_t}EndNode"
TAG_NODE_ID = f"{_t}NodeID"
TAG_NODE_ID_TYPE = f"{_t}NodeIDType"
TAG_REL_TYPE = f"{_t}RelationshipType"
TAG_REL_STATUS = f"{_t}RelationshipStatus"


def resolve_latest_url() -> str:
    """Query the GLEIF API for the latest Level 2 file URL."""
    resp = httpx.get(f"{GLEIF_RR_API}?page=0&pageSize=1", timeout=30)
    resp.raise_for_status()
    data = resp.json()["data"]
    if not data:
        raise RuntimeError("GLEIF API returned no relationship files")
    file_id = data[0]["id"]
    return f"{GLEIF_RR_API}/get/{file_id}/zip"


def download_zip(url: str) -> io.BytesIO:
    """Download a ZIP file into memory."""
    logger.info("Downloading %s ...", url)
    buf = io.BytesIO()
    with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        total = 0
        for chunk in r.iter_bytes(chunk_size=256 * 1024):
            buf.write(chunk)
            total += len(chunk)
    buf.seek(0)
    logger.info("Downloaded %.0f MB", total / 1e6)
    return buf


def _text(parent, tag):
    """Get text of a child element."""
    el = parent.find(tag)
    return el.text.strip() if el is not None and el.text else None


def parse_relationships(xml_stream):
    """Yield (child_lei, parent_lei, rel_type, status) from the XML."""
    for event, elem in iterparse(xml_stream, events=("end",)):
        if elem.tag != TAG_RECORD:
            continue

        rel = elem.find(TAG_RELATIONSHIP)
        if rel is None:
            elem.clear()
            continue

        start_node = rel.find(TAG_START_NODE)
        end_node = rel.find(TAG_END_NODE)
        if start_node is None or end_node is None:
            elem.clear()
            continue

        child_lei = _text(start_node, TAG_NODE_ID)
        parent_lei = _text(end_node, TAG_NODE_ID)
        rel_type = _text(rel, TAG_REL_TYPE)
        status = _text(rel, TAG_REL_STATUS)

        elem.clear()

        if not child_lei or not parent_lei:
            continue
        if status and status != "ACTIVE":
            continue

        # Map GLEIF relationship types to our simplified types
        if rel_type == "IS_DIRECTLY_CONSOLIDATED_BY":
            yield child_lei, parent_lei, "direct"
        elif rel_type == "IS_ULTIMATELY_CONSOLIDATED_BY":
            yield child_lei, parent_lei, "ultimate"


def load_relationships(driver, records, batch_size=BATCH_SIZE):
    """MERGE [:SUBSIDIARY_OF] relationships into Neo4j."""
    query = """
    UNWIND $batch AS row
    MATCH (child:Company {lei: row.child_lei})
    MATCH (parent:Company {lei: row.parent_lei})
    MERGE (child)-[r:SUBSIDIARY_OF {type: row.rel_type}]->(parent)
    """

    total = 0
    batch = []
    t0 = time.time()

    # Ensure LEI index exists (critical for MATCH performance)
    with driver.session() as session:
        session.run(
            "CREATE INDEX company_lei IF NOT EXISTS "
            "FOR (c:Company) ON (c.lei)"
        )

    with driver.session() as session:
        for child_lei, parent_lei, rel_type in records:
            batch.append({
                "child_lei": child_lei,
                "parent_lei": parent_lei,
                "rel_type": rel_type,
            })
            if len(batch) >= batch_size:
                session.run(query, batch=batch)
                total += len(batch)
                batch = []
                if total % 50000 < batch_size:
                    elapsed = time.time() - t0
                    rate = total / elapsed if elapsed else 0
                    logger.info(
                        "  %d relationships loaded (%.0f/s)", total, rate,
                    )

        if batch:
            session.run(query, batch=batch)
            total += len(batch)

    elapsed = time.time() - t0
    logger.info("Done: %d relationships in %.1fs", total, elapsed)
    return {"total": total, "elapsed_s": round(elapsed, 1)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load GLEIF Level 2 relationships into Neo4j",
    )
    parser.add_argument("--file", help="Path to a local GLEIF RR ZIP file")
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "gmr-neo4j-2026"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password),
    )
    try:
        with zf.open(xml_name) as xml_stream:
            records = parse_relationships(xml_stream)
            load_relationships(driver, records)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
