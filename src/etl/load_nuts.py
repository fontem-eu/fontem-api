"""
NUTS Region Hierarchy → Neo4j
=============================
Loads the NUTS (Nomenclature of Territorial Units for Statistics)
hierarchy into Neo4j as NUTSRegion nodes with PART_OF relationships,
then links existing Company and Authority nodes to their NUTS 0 region
based on the ``country`` property.

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
SET n.name  = row.name,
    n.level = row.level
"""

MERGE_PART_OF = """
UNWIND $batch AS row
WITH row WHERE row.parent IS NOT NULL
MATCH (child:NUTSRegion {code: row.code})
MATCH (parent:NUTSRegion {code: row.parent})
MERGE (child)-[:PART_OF]->(parent)
"""

LINK_COMPANIES = """
MATCH (c:Company), (n:NUTSRegion {level: 0})
WHERE c.country IS NOT NULL
  AND c.country = n.code
  AND NOT (c)-[:LOCATED_IN]->(:NUTSRegion)
MERGE (c)-[:LOCATED_IN]->(n)
"""

LINK_AUTHORITIES = """
MATCH (a:Authority), (n:NUTSRegion {level: 0})
WHERE a.country IS NOT NULL
  AND a.country = n.code
  AND NOT (a)-[:LOCATED_IN]->(:NUTSRegion)
MERGE (a)-[:LOCATED_IN]->(n)
"""

# EU-27 + EEA/candidate countries — NUTS level 0 codes
NUTS0_COUNTRIES = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria",
    "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany",
    "DK": "Denmark", "EE": "Estonia", "EL": "Greece",
    "ES": "Spain", "FI": "Finland", "FR": "France",
    "HR": "Croatia", "HU": "Hungary", "IE": "Ireland",
    "IT": "Italy", "LT": "Lithuania", "LU": "Luxembourg",
    "LV": "Latvia", "MT": "Malta", "NL": "Netherlands",
    "PL": "Poland", "PT": "Portugal", "RO": "Romania",
    "SE": "Sweden", "SI": "Slovenia", "SK": "Slovakia",
    "AL": "Albania", "CH": "Switzerland", "IS": "Iceland",
    "LI": "Liechtenstein", "ME": "Montenegro", "MK": "North Macedonia",
    "NO": "Norway", "RS": "Serbia", "TR": "Turkey",
    "UK": "United Kingdom", "BA": "Bosnia and Herzegovina",
    "XK": "Kosovo",
}


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
        yield {
            "code": code,
            "name": name or code,
            "level": level,
            "parent": _parent_code(code),
        }


def generate_nuts0_fallback():
    """Generate NUTS level 0 regions from hardcoded EU country codes."""
    for code, name in sorted(NUTS0_COUNTRIES.items()):
        yield {
            "code": code,
            "name": name,
            "level": 0,
            "parent": None,
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

        # Link Company and Authority nodes to NUTS 0
        logger.info("Linking Company nodes to NUTS 0 regions ...")
        result = session.run(LINK_COMPANIES)
        companies_linked = result.consume().counters.relationships_created
        logger.info("  linked %d companies", companies_linked)

        logger.info("Linking Authority nodes to NUTS 0 regions ...")
        result = session.run(LINK_AUTHORITIES)
        authorities_linked = result.consume().counters.relationships_created
        logger.info("  linked %d authorities", authorities_linked)

    elapsed = time.time() - t0
    return {
        "total": total,
        "companies_linked": companies_linked,
        "authorities_linked": authorities_linked,
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

    # Load regions
    if args.file:
        logger.info("Reading local file: %s", args.file)
        try:
            with open(args.file, encoding="utf-8") as fh:
                csv_text = fh.read()
        except OSError:
            logger.exception("Failed to read file %s", args.file)
            sys.exit(1)
        regions = list(parse_nuts_csv(csv_text))
    else:
        try:
            csv_text = download_nuts_csv()
            regions = list(parse_nuts_csv(csv_text))
        except (httpx.HTTPError, ValueError):
            logger.warning(
                "Failed to download NUTS CSV, falling back to NUTS 0 only"
            )
            regions = list(generate_nuts0_fallback())

    logger.info("Parsed %d NUTS regions", len(regions))

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = load_into_neo4j(driver, regions)
    finally:
        driver.close()

    logger.info(
        "Done: %d regions, %d companies linked, %d authorities linked in %.1fs",
        summary["total"],
        summary["companies_linked"],
        summary["authorities_linked"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
