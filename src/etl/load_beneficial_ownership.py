"""
OpenOwnership BODS (Beneficial Ownership Data Standard) → Neo4j
===============================================================
Downloads (or reads a local copy of) the GLEIF ownership subset from
OpenOwnership and MERGEs BeneficialOwner nodes with OWNS relationships
to existing Company nodes (matched via LEI).

Only natural-person records are loaded — company-to-company ownership
is already handled via SUBSIDIARY_OF relationships.

Usage:
    python -m src.etl.load_beneficial_ownership --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_beneficial_ownership --file /tmp/statements.latest.csv.gz
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import logging
import os
import sys
import time

import httpx
from neo4j import GraphDatabase

from . import gmr_id

logger = logging.getLogger(__name__)

BODS_URL = (
    "https://bods-data.openownership.org/storage/data/"
    "public/gleif/statements.latest.csv.gz"
)
BATCH_SIZE = 2000

CONSTRAINT_CYPHER = """
CREATE CONSTRAINT bo_id IF NOT EXISTS
FOR (b:BeneficialOwner) REQUIRE b.bo_id IS UNIQUE
"""

MERGE_OWNER = """
UNWIND $batch AS row
MERGE (b:BeneficialOwner {bo_id: row.bo_id})
SET b.name        = row.name,
    b.nationality = row.nationality,
    b.country     = row.country
"""

CREATE_OWNS = """
UNWIND $batch AS row
MATCH (b:BeneficialOwner {bo_id: row.bo_id})
MATCH (c:Company {lei: row.lei})
MERGE (b)-[r:OWNS]->(c)
SET r.interest_type    = row.interest_type,
    r.share_percentage = row.share_percentage,
    r.start_date       = row.start_date
"""


def download_bods():
    """Download the gzipped CSV from OpenOwnership."""
    logger.info("Downloading BODS data from %s ...", BODS_URL)
    try:
        resp = httpx.get(BODS_URL, timeout=600, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to download BODS data")
        sys.exit(1)
    logger.info("Downloaded %d MB", len(resp.content) // (1024 * 1024))
    return resp.content


def parse_bods_csv(data_bytes, is_gzipped=True):
    """
    Parse BODS CSV (possibly gzipped) and yield person-ownership dicts.

    Filters to natural persons only.
    """
    if is_gzipped:
        text_stream = io.TextIOWrapper(
            gzip.GzipFile(fileobj=io.BytesIO(data_bytes)),
            encoding="utf-8",
            errors="replace",
        )
    else:
        text_stream = io.StringIO(data_bytes.decode("utf-8", errors="replace"))

    reader = csv.DictReader(text_stream)
    for row in reader:
        # Only natural persons
        subject_type = (row.get("subjectType") or row.get("statementType") or "")
        if "person" not in subject_type.lower():
            continue

        subject_id = row.get("statementID") or row.get("id") or ""
        if not subject_id:
            continue

        lei = row.get("interestedParty_describedByEntityStatement_entityLEI") or ""
        if not lei:
            lei = row.get("lei") or row.get("LEI") or ""
        if not lei or len(lei) != 20:
            continue

        name = (
            row.get("personName") or row.get("name") or row.get("fullName") or ""
        ).strip()
        nationality = (row.get("nationality") or "").strip()
        country = (row.get("country") or row.get("addressCountry") or "").strip()

        interest_type = (row.get("interestType") or "").strip()
        share_pct_raw = row.get("sharePercentage") or row.get("interestLevel") or ""
        try:
            share_percentage = float(share_pct_raw) if share_pct_raw else None
        except ValueError:
            share_percentage = None

        start_date = (row.get("interestStartDate") or row.get("startDate") or "")[:10]

        bo_id = str(gmr_id.from_name("BO", f"bo:{subject_id}"))

        yield {
            "bo_id": bo_id,
            "name": name,
            "nationality": nationality,
            "country": country,
            "lei": lei,
            "interest_type": interest_type,
            "share_percentage": share_percentage,
            "start_date": start_date,
        }


def load_into_neo4j(driver, records):
    """MERGE BeneficialOwner nodes and create OWNS relationships."""
    total = 0
    linked = 0
    batch = []
    t0 = time.time()

    with driver.session() as session:
        session.run(CONSTRAINT_CYPHER)
        logger.info("Constraint ensured")

        for rec in records:
            batch.append(rec)
            if len(batch) >= BATCH_SIZE:
                session.run(MERGE_OWNER, batch=batch)
                result = session.run(CREATE_OWNS, batch=batch)
                linked += result.consume().counters.relationships_created
                total += len(batch)
                batch = []
                if total % 50000 < BATCH_SIZE:
                    elapsed = time.time() - t0
                    rate = total / elapsed if elapsed else 0
                    logger.info(
                        "  %d owners loaded (%.0f/s), %d linked",
                        total, rate, linked,
                    )

        if batch:
            session.run(MERGE_OWNER, batch=batch)
            result = session.run(CREATE_OWNS, batch=batch)
            linked += result.consume().counters.relationships_created
            total += len(batch)

    elapsed = time.time() - t0
    return {"total": total, "linked": linked, "elapsed_s": round(elapsed, 1)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load OpenOwnership BODS beneficial ownership into Neo4j"
    )
    parser.add_argument(
        "--file", help="Path to local CSV file (gzipped or plain)"
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

    if args.file:
        logger.info("Reading local file: %s", args.file)
        try:
            with open(args.file, "rb") as fh:
                data_bytes = fh.read()
        except OSError:
            logger.exception("Failed to read %s", args.file)
            sys.exit(1)
        is_gzipped = args.file.endswith(".gz")
    else:
        data_bytes = download_bods()
        is_gzipped = True

    records = list(parse_bods_csv(data_bytes, is_gzipped=is_gzipped))
    logger.info("Parsed %d beneficial-owner records", len(records))

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = load_into_neo4j(driver, records)
    finally:
        driver.close()

    logger.info(
        "Done: %d owners, %d OWNS relationships in %.1fs",
        summary["total"],
        summary["linked"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
