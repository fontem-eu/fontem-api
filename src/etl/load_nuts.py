"""
NUTS Region Hierarchy → Neo4j
=============================
Loads the NUTS (Nomenclature of Territorial Units for Statistics) hierarchy
into Neo4j as NUTSRegion nodes with PART_OF relationships covering all four
levels (0: countries, 1: major regions, 2: basic regions, 3: small regions).

Entity → region linking is a separate concern (see ``link_entities_to_nuts``);
this script only populates the reference hierarchy.

Usage:
    python -m src.etl.load_nuts --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_nuts --file /tmp/NUTS2024.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import time

import httpx
from neo4j import GraphDatabase

from src.services.location_service import LocationService

logger = logging.getLogger(__name__)

NUTS_CSV_URL = (
    "https://ec.europa.eu/eurostat/cache/GISCO/distribution/"
    "v2/nuts/csv/NUTS_AT_2024.csv"
)

BATCH_SIZE = 500

CONSTRAINT_CYPHER = """
CREATE CONSTRAINT nuts_code IF NOT EXISTS
FOR (n:NUTSRegion) REQUIRE n.code IS UNIQUE
"""

MERGE_REGION = """
UNWIND $batch AS row
MERGE (n:NUTSRegion {code: row.code})
SET n.name           = row.name,
    n.level          = row.level,
    n.country_alpha3 = row.country_alpha3
"""

MERGE_PART_OF = """
UNWIND $batch AS row
WITH row WHERE row.parent IS NOT NULL
MATCH (child:NUTSRegion {code: row.code})
MATCH (parent:NUTSRegion {code: row.parent})
MERGE (child)-[:PART_OF]->(parent)
"""


def _parent_code(code: str) -> str | None:
    """Derive the parent NUTS code by removing the last character."""
    if len(code) <= 2:
        return None
    return code[:-1]


def parse_nuts_csv(csv_text: str):
    """
    Parse a CSV with at least a ``NUTS_ID`` (or ``code``) column.

    Yields dicts with keys: code, name, level, parent.
    """
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=",")
    fieldnames = [f.strip().strip("\ufeff") for f in (reader.fieldnames or [])]
    reader.fieldnames = fieldnames

    code_col = None
    name_col = None
    for col in fieldnames:
        upper = col.upper()
        if upper in ("NUTS_ID", "CODE"):
            code_col = col
        if upper in ("NUTS_NAME", "NAME", "LABEL", "DESCRIPTION"):
            name_col = col

    if code_col is None:
        raise ValueError(
            f"CSV must have a NUTS_ID or CODE column, got: {fieldnames}"
        )

    for row in reader:
        code = (row.get(code_col) or "").strip()
        if not code or len(code) < 2 or len(code) > 5:
            continue
        name = (row.get(name_col) or "").strip() if name_col else ""
        level = len(code) - 2
        country_alpha3 = LocationService.country_from_nuts(code) or ""
        yield {
            "code": code,
            "name": name or code,
            "level": level,
            "parent": _parent_code(code),
            "country_alpha3": country_alpha3,
        }


def download_nuts_csv() -> str:
    """Download the NUTS CSV from Eurostat."""
    logger.info("Downloading NUTS CSV from %s", NUTS_CSV_URL)
    resp = httpx.get(NUTS_CSV_URL, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def load_into_neo4j(driver, regions):
    """MERGE NUTSRegion nodes and PART_OF relationships in batches."""
    total = 0
    batch = []
    all_regions = []
    t0 = time.time()

    with driver.session() as session:
        session.run(CONSTRAINT_CYPHER)
        logger.info("Constraint ensured")

        for region in regions:
            batch.append(region)
            all_regions.append(region)

            if len(batch) >= BATCH_SIZE:
                session.run(MERGE_REGION, batch=batch)
                total += len(batch)
                batch = []
                if total % 5000 < BATCH_SIZE:
                    logger.info("  %d regions loaded", total)

        if batch:
            session.run(MERGE_REGION, batch=batch)
            total += len(batch)

        # Create PART_OF relationships in batches
        logger.info("Creating PART_OF relationships ...")
        part_of_batch = []
        for region in all_regions:
            if region["parent"] is not None:
                part_of_batch.append(region)
                if len(part_of_batch) >= BATCH_SIZE:
                    session.run(MERGE_PART_OF, batch=part_of_batch)
                    part_of_batch = []
        if part_of_batch:
            session.run(MERGE_PART_OF, batch=part_of_batch)

    elapsed = time.time() - t0
    by_level = {0: 0, 1: 0, 2: 0, 3: 0}
    for r in all_regions:
        by_level[r["level"]] = by_level.get(r["level"], 0) + 1
    return {
        "total": total,
        "by_level": by_level,
        "elapsed_s": round(elapsed, 1),
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load NUTS region hierarchy into Neo4j"
    )
    parser.add_argument(
        "--file",
        help="Path to a local CSV with NUTS_ID and NUTS_NAME columns",
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

    # Load regions — fail loudly on download errors; masking with a NUTS 0
    # fallback hid a real production problem for months.
    if args.file:
        logger.info("Reading local file: %s", args.file)
        try:
            with open(args.file, encoding="utf-8") as fh:
                csv_text = fh.read()
        except OSError:
            logger.exception("Failed to read file %s", args.file)
            sys.exit(1)
    else:
        csv_text = download_nuts_csv()

    regions = list(parse_nuts_csv(csv_text))
    if not regions:
        logger.error("Parsed zero regions from CSV — aborting")
        sys.exit(1)
    logger.info("Parsed %d NUTS regions", len(regions))

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = load_into_neo4j(driver, regions)
    finally:
        driver.close()

    logger.info(
        "Done: %d regions loaded in %.1fs (by level: %s)",
        summary["total"],
        summary["elapsed_s"],
        summary["by_level"],
    )


if __name__ == "__main__":
    main()
