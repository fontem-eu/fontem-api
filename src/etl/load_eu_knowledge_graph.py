"""
EU Knowledge Graph (Kohesio) → Neo4j
=====================================
Ingests EU cohesion policy projects and beneficiaries from the
Kohesio per-country CSV exports into Neo4j as CohesionProject nodes.
Matches beneficiaries against existing Company nodes and links
projects to NUTSRegion nodes.

Data source: per-country CSV files from the official Kohesio data
export API, NOT the SPARQL endpoint (which is for targeted queries).

URL pattern:
  https://kohesio.ec.europa.eu/api/data/object?id=data/projects-2021-2027/latest/{CC}-pp21-27-latest.csv

Usage:
    python -m src.etl.load_eu_knowledge_graph
    python -m src.etl.load_eu_knowledge_graph --countries PT,FR,DE
    python -m src.etl.load_eu_knowledge_graph --file /tmp/PT-pp21-27-latest.csv
    python -m src.etl.load_eu_knowledge_graph --since 2025-09-01
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

from . import gmr_id

logger = logging.getLogger(__name__)

KOHESIO_CSV_URL = (
    "https://kohesio.ec.europa.eu/api/data/object"
    "?id=data/projects-2021-2027/latest/{cc}-pp21-27-latest.csv"
)

EU_COUNTRIES = [
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES",
    "FI", "FR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT",
    "NL", "PL", "PT", "RO", "SE", "SI", "SK",
]

BATCH_SIZE = 500

CONSTRAINT_CYPHER = """
CREATE CONSTRAINT cohesion_project_id IF NOT EXISTS
FOR (p:CohesionProject) REQUIRE p.project_id IS UNIQUE
"""

MERGE_PROJECT = """
UNWIND $batch AS row
MERGE (p:CohesionProject {project_id: row.project_id})
SET p.wikibase_qid    = row.wikibase_qid,
    p.title           = row.title,
    p.description     = row.description,
    p.total_budget    = row.total_budget,
    p.eu_contribution = row.eu_contribution,
    p.fund            = row.fund,
    p.programme       = row.programme,
    p.start_date      = row.start_date,
    p.end_date        = row.end_date,
    p.nuts_code       = row.nuts_code,
    p.country         = row.country
"""

LINK_NUTS = """
UNWIND $batch AS row
WITH row WHERE row.nuts_code IS NOT NULL AND row.nuts_code <> ''
MATCH (p:CohesionProject {project_id: row.project_id})
OPTIONAL MATCH (exact:NUTSRegion {code: row.nuts_code})
OPTIONAL MATCH (parent2:NUTSRegion)
  WHERE parent2.code = left(row.nuts_code, size(row.nuts_code) - 1)
    AND exact IS NULL
OPTIONAL MATCH (parent1:NUTSRegion)
  WHERE parent1.code = left(row.nuts_code, size(row.nuts_code) - 2)
    AND exact IS NULL AND parent2 IS NULL
WITH p, coalesce(exact, parent2, parent1) AS region
WHERE region IS NOT NULL
MERGE (p)-[:LOCATED_IN]->(region)
"""

MERGE_BENEFICIARY = """
UNWIND $batch AS row
WITH row WHERE row.beneficiary_gmr_id IS NOT NULL
MATCH (p:CohesionProject {project_id: row.project_id})
MERGE (c:Company {gmr_id: row.beneficiary_gmr_id})
ON CREATE SET c.name    = row.beneficiary_name,
              c.country = row.country
MERGE (c)-[:BENEFICIARY_OF]->(p)
"""


def _extract_qid(uri: str) -> str:
    """Extract a Wikibase QID from a URI like .../entity/Q123."""
    if not uri:
        return ""
    part = uri.rsplit("/", maxsplit=1)[-1]
    if part.startswith("Q") and part[1:].isdigit():
        return part
    return ""


def _normalize_date(raw: str) -> str:
    """Convert DD/MM/YYYY to YYYY-MM-DD. Returns '' on failure."""
    raw = (raw or "").strip()[:10]
    if not raw:
        return ""
    # Already ISO? (starts with 4-digit year)
    if len(raw) >= 10 and raw[4] == "-":
        return raw[:10]
    # DD/MM/YYYY
    parts = raw.split("/")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return raw


def _to_float(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def download_country_csv(country_code: str) -> bytes:
    """Download a single country's CSV from Kohesio."""
    url = KOHESIO_CSV_URL.format(cc=country_code)
    logger.info("Downloading %s ...", url)
    resp = httpx.get(url, timeout=300, follow_redirects=True,
                     headers={"User-Agent": "GMR-KnowledgeGraph/1.0"})
    resp.raise_for_status()
    logger.info("  %s: %d KB", country_code, len(resp.content) // 1024)
    return resp.content


def parse_kohesio_csv(data_bytes: bytes, since: str | None = None):
    """Parse a Kohesio CSV and yield project dicts."""
    text = io.StringIO(data_bytes.decode("utf-8", errors="replace"))
    reader = csv.DictReader(text)

    for row in reader:
        # Normalize DD/MM/YYYY → YYYY-MM-DD for comparison and storage
        start_date = _normalize_date(row.get("Operation_Start_Date", ""))
        end_date = _normalize_date(row.get("Operation_End_Date", ""))

        # Use the best available date for --since filtering:
        # prefer start_date, fall back to end_date, skip if both missing
        filter_date = start_date or end_date
        if since:
            if not filter_date:
                continue  # no temporal info at all — cannot determine relevance
            if filter_date < since:
                continue

        # Extract QID from the Operation_Unique_Identifier URI
        op_uri = row.get("Operation_Unique_Identifier", "")
        qid = _extract_qid(op_uri)
        if not qid:
            continue

        project_id = str(gmr_id.from_name("EU", f"eukg:{qid}"))
        raw_country = (row.get("CountryCode") or "")[:5].strip()
        country_code = LocationService.to_alpha3(raw_country) or raw_country

        # Best NUTS code: prefer NUTS3, then NUTS2, then NUTS1
        nuts_code = (
            row.get("NUTS3_Code")
            or row.get("NUTS2_Code")
            or row.get("NUTS1_Code")
            or ""
        ).strip()

        # Beneficiary
        beneficiary_name = None
        beneficiary_gmr_id = None
        ben_uri = row.get("Beneficiary_Unique_Identifier", "")
        ben_qid = _extract_qid(ben_uri)
        if ben_qid:
            # Use the QID-based ID for the beneficiary company
            beneficiary_gmr_id = str(
                gmr_id.from_name(country_code or "EU", f"kohesio_ben:{ben_qid}")
            )
            # No name available in the CSV for beneficiaries (just URI)
            beneficiary_name = ben_qid

        yield {
            "project_id": project_id,
            "wikibase_qid": qid,
            "title": (
                row.get("Operation_Name_English")
                or row.get("Operation_Name_Programme_Language")
                or ""
            )[:500] or None,
            "description": (
                row.get("Operation_Summary_English")
                or row.get("Operation_Summary_Programme_Language")
                or ""
            )[:2000] or None,
            "total_budget": _to_float(
                row.get("Total_Eligible_Expenditure_amount", "")
            ),
            "eu_contribution": _to_float(
                row.get("Project_EU_Budget", "")
            ),
            "fund": (row.get("Fund_Name") or row.get("Fund_Code") or "")[:200] or None,
            "programme": (row.get("Programme_Name") or "")[:200] or None,
            "start_date": start_date or None,
            "end_date": end_date or None,
            "nuts_code": nuts_code or None,
            "country": country_code or None,
            "beneficiary_gmr_id": beneficiary_gmr_id,
            "beneficiary_name": beneficiary_name,
        }


def load_into_neo4j(driver, records):
    """MERGE CohesionProject nodes and beneficiary relationships."""
    total = 0
    batch = []
    t0 = time.time()

    with driver.session() as session:
        session.run(CONSTRAINT_CYPHER)
        logger.info("Constraint ensured")

        for record in records:
            batch.append(record)
            if len(batch) >= BATCH_SIZE:
                session.run(MERGE_PROJECT, batch=batch)
                session.run(LINK_NUTS, batch=batch)
                session.run(MERGE_BENEFICIARY, batch=batch)
                total += len(batch)
                batch = []
                if total % 10000 < BATCH_SIZE:
                    elapsed = time.time() - t0
                    rate = total / max(elapsed, 0.1)
                    logger.info("  %d projects loaded (%.0f/s)", total, rate)

        if batch:
            session.run(MERGE_PROJECT, batch=batch)
            session.run(LINK_NUTS, batch=batch)
            session.run(MERGE_BENEFICIARY, batch=batch)
            total += len(batch)

    elapsed = time.time() - t0
    return {"total": total, "elapsed_s": round(elapsed, 1)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load EU Knowledge Graph cohesion projects into Neo4j"
    )
    parser.add_argument(
        "--file", help="Path to a local Kohesio CSV file",
    )
    parser.add_argument(
        "--countries",
        default=",".join(EU_COUNTRIES),
        help="Comma-separated country codes (default: all EU-27)",
    )
    parser.add_argument(
        "--since", default="2025-09-01",
        help="Only ingest projects with start_date >= YYYY-MM-DD",
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

    # Collect all records
    all_records = []

    if args.file:
        logger.info("Reading local file: %s", args.file)
        try:
            with open(args.file, "rb") as fh:
                data = fh.read()
        except OSError:
            logger.exception("Failed to read %s", args.file)
            sys.exit(1)
        all_records = list(parse_kohesio_csv(data, since=args.since))
    else:
        countries = [c.strip() for c in args.countries.split(",") if c.strip()]
        logger.info("Downloading %d countries (since=%s)", len(countries), args.since)
        for cc in countries:
            try:
                data = download_country_csv(cc)
                records = list(parse_kohesio_csv(data, since=args.since))
                logger.info("  %s: %d projects after date filter", cc, len(records))
                all_records.extend(records)
            except httpx.HTTPError:
                logger.warning("  %s: download failed, skipping", cc)

    logger.info("Total: %d projects to load", len(all_records))

    if not all_records:
        logger.info("No records to load, exiting")
        return

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = load_into_neo4j(driver, all_records)
        # Cumulative coverage range across runs.
        with driver.session() as session:
            rng = session.run(
                "MATCH (p:CohesionProject) "
                "WHERE p.start_date IS NOT NULL "
                "RETURN min(p.start_date) AS first, "
                "  max(coalesce(p.end_date, p.start_date)) AS last, "
                "  count(p) AS n"
            ).single()
        from src.etl import _freshness  # pylint: disable=import-outside-toplevel
        _freshness.update_source(
            driver,
            source_id="eu-knowledge-graph",
            label="EU Knowledge Graph cohesion projects",
            coverage_start=(rng["first"][:10] if rng and rng["first"] else None),
            coverage_end=(rng["last"][:10] if rng and rng["last"] else None),
            record_count=int(rng["n"]) if rng else summary.get("total", 0),
            expected_cadence_hours=24 * 35,  # monthly (15th of month)
        )
    finally:
        driver.close()

    logger.info(
        "Done: %d projects in %.1fs",
        summary["total"], summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
