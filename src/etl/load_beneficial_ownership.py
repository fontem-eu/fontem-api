"""
OpenOwnership BODS (Beneficial Ownership Data Standard) → Neo4j
===============================================================
Downloads (or reads a local copy of) the GLEIF ownership subset from
OpenOwnership and MERGEs ownership relationships into Neo4j.

Two types of records are handled:

1. **Entity-to-entity** (company-to-company) ownership — creates OWNS
   relationships between Company nodes matched via LEI.
2. **Person-to-entity** — creates BeneficialOwner nodes with OWNS
   relationships to Company nodes matched via LEI.

Usage:
    python -m src.etl.load_beneficial_ownership --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_beneficial_ownership --file /tmp/statements.latest.csv.gz
"""
from __future__ import annotations

import argparse
import csv
import zipfile
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
    "https://oo-bodsdata.s3.amazonaws.com/data/"
    "gleif_version_0_4/csv.zip"
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

MERGE_ENTITY_OWNS = """
UNWIND $batch AS row
MATCH (parent:Company {lei: row.parent_lei})
MATCH (child:Company {lei: row.child_lei})
MERGE (parent)-[r:OWNS]->(child)
SET r.interest_type    = row.interest_type,
    r.share_percentage = row.share_percentage,
    r.start_date       = row.start_date,
    r.source           = 'bods_gleif'
"""


def download_bods():
    """Download the CSV ZIP from OpenOwnership."""
    logger.info("Downloading BODS data from %s ...", BODS_URL)
    try:
        resp = httpx.get(BODS_URL, timeout=600, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to download BODS data")
        sys.exit(1)
    logger.info("Downloaded %d MB", len(resp.content) // (1024 * 1024))
    return resp.content


def _parse_share_pct(row):
    """Extract share percentage from various column names."""
    for col in ("sharePercentage", "interestLevel",
                "interests_share_exact", "interests_share_minimum"):
        raw = (row.get(col) or "").strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                continue
    return None


def _parse_person_row(row):
    """Parse a person-to-entity BODS CSV row, or None if not relevant."""
    subject_type = (row.get("subjectType") or row.get("statementType") or "")
    if "person" not in subject_type.lower():
        return None

    subject_id = row.get("statementID") or row.get("id") or ""
    if not subject_id:
        return None

    lei = row.get("interestedParty_describedByEntityStatement_entityLEI") or ""
    if not lei:
        lei = row.get("lei") or row.get("LEI") or ""
    if not lei or len(lei) != 20:
        return None

    return {
        "record_type": "person",
        "bo_id": str(gmr_id.from_name("BO", f"bo:{subject_id}")),
        "name": (
            row.get("personName") or row.get("name") or row.get("fullName") or ""
        ).strip(),
        "nationality": (row.get("nationality") or "").strip(),
        "country": (row.get("country") or row.get("addressCountry") or "").strip(),
        "lei": lei,
        "interest_type": (row.get("interestType") or "").strip(),
        "share_percentage": _parse_share_pct(row),
        "start_date": (
            row.get("interestStartDate") or row.get("startDate") or ""
        )[:10],
    }


def _parse_entity_row(row):
    """Parse an entity-to-entity BODS CSV row, or None if not relevant.

    The GLEIF BODS CSV has columns like:
    - ``interestedParty_describedByEntityStatement_entityLEI`` (parent LEI)
    - ``subject_describedByEntityStatement_entityLEI`` (child LEI, the owned entity)
    """
    subject_type = (row.get("subjectType") or row.get("statementType") or "")
    # Entity-to-entity rows have statementType containing 'ownership'
    # or subjectType containing 'entity'
    is_entity = (
        "entity" in subject_type.lower()
        or "ownership" in subject_type.lower()
    )
    # If it's explicitly a person, skip
    if "person" in subject_type.lower():
        return None
    if not is_entity:
        return None

    parent_lei = (
        row.get("interestedParty_describedByEntityStatement_entityLEI")
        or row.get("interestedPartyLEI")
        or ""
    ).strip()
    child_lei = (
        row.get("subject_describedByEntityStatement_entityLEI")
        or row.get("subjectLEI")
        or row.get("lei")
        or row.get("LEI")
        or ""
    ).strip()

    if not parent_lei or len(parent_lei) != 20:
        return None
    if not child_lei or len(child_lei) != 20:
        return None
    if parent_lei == child_lei:
        return None

    return {
        "record_type": "entity",
        "parent_lei": parent_lei,
        "child_lei": child_lei,
        "interest_type": (row.get("interestType") or "").strip(),
        "share_percentage": _parse_share_pct(row),
        "start_date": (
            row.get("interestStartDate") or row.get("startDate") or ""
        )[:10],
    }


def _parse_row(row):
    """Parse a single BODS CSV row — tries entity-to-entity first, then person."""
    parsed = _parse_entity_row(row)
    if parsed is not None:
        return parsed
    return _parse_person_row(row)


def parse_bods_csv(data_bytes, is_zip=True):
    """
    Parse BODS CSV data and yield ownership dicts.

    Handles ZIP archives (containing one or more CSVs) and plain CSV.
    Returns both person-to-entity and entity-to-entity records.
    """
    if is_zip:
        zf = zipfile.ZipFile(io.BytesIO(data_bytes))
        csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
        logger.info("ZIP contains %d CSV files: %s", len(csv_names),
                     csv_names[:5])
        for name in csv_names:
            text_stream = io.TextIOWrapper(
                zf.open(name), encoding="utf-8", errors="replace",
            )
            reader = csv.DictReader(text_stream)
            for row in reader:
                parsed = _parse_row(row)
                if parsed is not None:
                    yield parsed
    else:
        text_stream = io.StringIO(
            data_bytes.decode("utf-8", errors="replace")
        )
        reader = csv.DictReader(text_stream)
        for row in reader:
            parsed = _parse_row(row)
            if parsed is not None:
                yield parsed


def load_into_neo4j(driver, records):
    """MERGE ownership relationships into Neo4j.

    Handles both person-to-entity (BeneficialOwner -> Company) and
    entity-to-entity (Company -> Company) records.
    """
    total_person = 0
    total_entity = 0
    linked_person = 0
    linked_entity = 0
    person_batch = []
    entity_batch = []
    t0 = time.time()

    with driver.session() as session:
        session.run(CONSTRAINT_CYPHER)
        logger.info("Constraint ensured")

        for rec in records:
            if rec.get("record_type") == "entity":
                entity_batch.append(rec)
                if len(entity_batch) >= BATCH_SIZE:
                    result = session.run(MERGE_ENTITY_OWNS, batch=entity_batch)
                    linked_entity += result.consume().counters.relationships_created
                    total_entity += len(entity_batch)
                    entity_batch = []
            else:
                person_batch.append(rec)
                if len(person_batch) >= BATCH_SIZE:
                    session.run(MERGE_OWNER, batch=person_batch)
                    result = session.run(CREATE_OWNS, batch=person_batch)
                    linked_person += result.consume().counters.relationships_created
                    total_person += len(person_batch)
                    person_batch = []

            total = total_person + total_entity
            if total % 50000 < BATCH_SIZE and total > 0:
                elapsed = time.time() - t0
                rate = total / elapsed if elapsed else 0
                logger.info(
                    "  %d records (%.0f/s): %d person, %d entity",
                    total, rate, linked_person, linked_entity,
                )

        if person_batch:
            session.run(MERGE_OWNER, batch=person_batch)
            result = session.run(CREATE_OWNS, batch=person_batch)
            linked_person += result.consume().counters.relationships_created
            total_person += len(person_batch)

        if entity_batch:
            result = session.run(MERGE_ENTITY_OWNS, batch=entity_batch)
            linked_entity += result.consume().counters.relationships_created
            total_entity += len(entity_batch)

    elapsed = time.time() - t0
    return {
        "total": total_person + total_entity,
        "total_person": total_person,
        "total_entity": total_entity,
        "linked_person": linked_person,
        "linked_entity": linked_entity,
        "elapsed_s": round(elapsed, 1),
    }


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
        is_zip = args.file.endswith(".zip")
    else:
        data_bytes = download_bods()
        is_zip = True

    records = list(parse_bods_csv(data_bytes, is_zip=is_zip))
    logger.info("Parsed %d beneficial-owner records", len(records))

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = load_into_neo4j(driver, records)
    finally:
        driver.close()

    logger.info(
        "Done: %d records (%d person, %d entity), "
        "%d person links, %d entity links in %.1fs",
        summary["total"],
        summary["total_person"],
        summary["total_entity"],
        summary["linked_person"],
        summary["linked_entity"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
