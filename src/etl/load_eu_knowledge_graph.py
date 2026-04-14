"""
EU Knowledge Graph (Kohesio) → Neo4j
=====================================
Ingests EU cohesion policy projects and beneficiaries from the
LinkedOpenData SPARQL endpoint into Neo4j as CohesionProject nodes.
Matches beneficiaries against existing Company nodes and links
projects to NUTSRegion nodes.

Usage:
    python -m src.etl.load_eu_knowledge_graph --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_eu_knowledge_graph --file /tmp/eukg_projects.json
    python -m src.etl.load_eu_knowledge_graph --limit 1000 --since 2020-01-01
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from neo4j import GraphDatabase

from . import gmr_id

logger = logging.getLogger(__name__)

SPARQL_ENDPOINT = "https://query.linkedopendata.eu/sparql"
PAGE_SIZE = 10000
BATCH_SIZE = 500

CONSTRAINT_CYPHER = """
CREATE CONSTRAINT cohesion_project_id IF NOT EXISTS
FOR (p:CohesionProject) REQUIRE p.project_id IS UNIQUE
"""

MERGE_PROJECT = """
UNWIND $batch AS row
MERGE (p:CohesionProject {project_id: row.project_id})
SET p.wikibase_qid   = row.wikibase_qid,
    p.title          = row.title,
    p.description    = row.description,
    p.total_budget   = row.total_budget,
    p.eu_contribution = row.eu_contribution,
    p.fund           = row.fund,
    p.programme      = row.programme,
    p.start_date     = row.start_date,
    p.end_date       = row.end_date,
    p.nuts_code      = row.nuts_code,
    p.country        = row.country
"""

LINK_NUTS = """
UNWIND $batch AS row
WITH row WHERE row.nuts_code IS NOT NULL AND row.nuts_code <> ''
MATCH (p:CohesionProject {project_id: row.project_id})
MATCH (n:NUTSRegion {code: row.nuts_code})
MERGE (p)-[:LOCATED_IN]->(n)
"""

MERGE_BENEFICIARY_EXACT = """
UNWIND $batch AS row
WITH row WHERE row.wikidata_qid IS NOT NULL AND row.wikidata_qid <> ''
MATCH (c:Company {wikidata_qid: row.wikidata_qid})
MATCH (p:CohesionProject {project_id: row.project_id})
MERGE (c)-[:BENEFICIARY_OF]->(p)
"""

MERGE_BENEFICIARY_BY_NAME = """
UNWIND $batch AS row
WITH row WHERE row.beneficiary_name IS NOT NULL AND row.beneficiary_name <> ''
MATCH (p:CohesionProject {project_id: row.project_id})
WHERE NOT ()-[:BENEFICIARY_OF]->(p)
MERGE (c:Company {gmr_id: row.beneficiary_gmr_id})
ON CREATE SET c.name    = row.beneficiary_name,
              c.country = row.beneficiary_country
MERGE (c)-[:BENEFICIARY_OF]->(p)
"""

PROJECT_SPARQL = """
SELECT ?project ?title ?description ?totalBudget ?euContribution
       ?startDate ?endDate ?fund ?programme ?nuts ?country
WHERE {{
  ?project wdt:P1 wd:Q9934 .
  OPTIONAL {{ ?project rdfs:label ?title . FILTER(LANG(?title) = "en") }}
  OPTIONAL {{ ?project schema:description ?description .
              FILTER(LANG(?description) = "en") }}
  OPTIONAL {{ ?project wdt:P835 ?totalBudget }}
  OPTIONAL {{ ?project wdt:P836 ?euContribution }}
  OPTIONAL {{ ?project wdt:P580 ?startDate }}
  OPTIONAL {{ ?project wdt:P582 ?endDate }}
  OPTIONAL {{ ?project wdt:P1584 ?fund }}
  OPTIONAL {{ ?project wdt:P1368 ?programme }}
  OPTIONAL {{ ?project wdt:P7 ?nuts }}
  OPTIONAL {{ ?project wdt:P35 ?country }}
  {since_filter}
}}
LIMIT {limit} OFFSET {offset}
"""

BENEFICIARY_SPARQL = """
SELECT ?project ?beneficiary ?beneficiaryLabel
       ?beneficiaryCountry ?wikidata
WHERE {{
  ?project wdt:P1 wd:Q9934 .
  ?project wdt:P708 ?beneficiary .
  OPTIONAL {{ ?beneficiary rdfs:label ?beneficiaryLabel .
              FILTER(LANG(?beneficiaryLabel) = "en") }}
  OPTIONAL {{ ?beneficiary wdt:P35 ?beneficiaryCountry }}
  OPTIONAL {{ ?beneficiary owl:sameAs ?wikidata .
              FILTER(CONTAINS(STR(?wikidata), "wikidata.org")) }}
}}
LIMIT {limit} OFFSET {offset}
"""

_QID_RE = re.compile(r"(Q\d+)$")


def _extract_qid(uri: str) -> str:
    """Extract a Wikibase QID from a URI like http://...entity/Q123."""
    match = _QID_RE.search(uri)
    return match.group(1) if match else ""


def _get_value(binding: dict, key: str) -> str:
    """Safely extract a value from a SPARQL JSON binding."""
    entry = binding.get(key)
    if entry is None:
        return ""
    return entry.get("value", "")


def _to_float(value: str) -> float | None:
    """Convert a string to float, returning None on failure."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def sparql_query(query: str, retries: int = 3) -> dict:
    """Execute a SPARQL query with retry logic and return JSON results."""
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{SPARQL_ENDPOINT}?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": "GMR-KnowledgeGraph/1.0 (civic-transparency; contact@gmr.void42.net)",
        },
    )

    delays = [5, 15, 45]
    last_error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries - 1:
                delay = delays[min(attempt, len(delays) - 1)]
                logger.warning(
                    "SPARQL query failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, retries, delay, exc,
                )
                time.sleep(delay)

    raise RuntimeError(
        f"SPARQL query failed after {retries} attempts"
    ) from last_error


def _extract_nuts_code(nuts_uri: str) -> str:
    """Extract a NUTS code from a URI or raw value."""
    if not nuts_uri:
        return ""
    # URI like http://.../entity/Q... → not useful as NUTS code
    # Some endpoints return the code directly
    code = nuts_uri.rsplit("/", maxsplit=1)[-1]
    # If it looks like a QID, it's not a NUTS code
    if code.startswith("Q") and code[1:].isdigit():
        return ""
    return code


def fetch_projects(since: str | None = None, max_projects: int | None = None):
    """Fetch projects from the SPARQL endpoint with pagination."""
    since_filter = ""
    if since:
        since_filter = (
            f'FILTER(!BOUND(?startDate) || ?startDate >= "{since}"^^xsd:date)'
        )

    offset = 0
    total_fetched = 0
    while True:
        if max_projects is not None:
            page = min(PAGE_SIZE, max_projects - total_fetched)
            if page <= 0:
                break
        else:
            page = PAGE_SIZE

        query = PROJECT_SPARQL.format(
            since_filter=since_filter, limit=page, offset=offset
        )
        result = sparql_query(query)
        bindings = result.get("results", {}).get("bindings", [])

        if not bindings:
            break

        for row in bindings:
            project_uri = _get_value(row, "project")
            qid = _extract_qid(project_uri)
            if not qid:
                continue

            project_id = str(
                gmr_id.from_name("EU", f"eukg:{qid}")
            )

            yield {
                "project_id": project_id,
                "wikibase_qid": qid,
                "title": _get_value(row, "title")[:500] or None,
                "description": _get_value(row, "description")[:2000] or None,
                "total_budget": _to_float(_get_value(row, "totalBudget")),
                "eu_contribution": _to_float(
                    _get_value(row, "euContribution")
                ),
                "fund": _get_value(row, "fund")[:200] or None,
                "programme": _get_value(row, "programme")[:200] or None,
                "start_date": _get_value(row, "startDate")[:10] or None,
                "end_date": _get_value(row, "endDate")[:10] or None,
                "nuts_code": _extract_nuts_code(
                    _get_value(row, "nuts")
                ) or None,
                "country": _get_value(row, "country")[:5] or None,
            }

        total_fetched += len(bindings)
        if total_fetched % 10000 < PAGE_SIZE:
            logger.info("  fetched %d projects so far", total_fetched)

        if len(bindings) < page:
            break
        offset += page

    logger.info("Fetched %d projects total", total_fetched)


def fetch_beneficiaries(max_records: int | None = None):
    """Fetch beneficiary-project links from the SPARQL endpoint."""
    offset = 0
    total_fetched = 0
    while True:
        if max_records is not None:
            page = min(PAGE_SIZE, max_records - total_fetched)
            if page <= 0:
                break
        else:
            page = PAGE_SIZE

        query = BENEFICIARY_SPARQL.format(limit=page, offset=offset)
        result = sparql_query(query)
        bindings = result.get("results", {}).get("bindings", [])

        if not bindings:
            break

        for row in bindings:
            project_uri = _get_value(row, "project")
            project_qid = _extract_qid(project_uri)
            if not project_qid:
                continue

            project_id = str(
                gmr_id.from_name("EU", f"eukg:{project_qid}")
            )
            beneficiary_name = _get_value(row, "beneficiaryLabel")
            beneficiary_country = _get_value(row, "beneficiaryCountry")[:5]
            wikidata_uri = _get_value(row, "wikidata")
            wikidata_qid = _extract_qid(wikidata_uri) if wikidata_uri else ""

            yield {
                "project_id": project_id,
                "beneficiary_name": beneficiary_name or None,
                "beneficiary_country": beneficiary_country or None,
                "wikidata_qid": wikidata_qid or None,
                "beneficiary_gmr_id": (
                    gmr_id.from_name(
                        beneficiary_country or "EU",
                        beneficiary_name,
                    )
                    if beneficiary_name
                    else None
                ),
            }

        total_fetched += len(bindings)
        if total_fetched % 10000 < PAGE_SIZE:
            logger.info("  fetched %d beneficiaries so far", total_fetched)

        if len(bindings) < page:
            break
        offset += page

    logger.info("Fetched %d beneficiaries total", total_fetched)


def load_projects_from_file(path: str):
    """Load projects from a local JSON file."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    projects = data if isinstance(data, list) else data.get("projects", [])
    for proj in projects:
        qid = proj.get("wikibase_qid", "")
        if not qid:
            continue
        proj["project_id"] = str(
            gmr_id.from_name("EU", f"eukg:{qid}")
        )
        yield proj


def load_into_neo4j(driver, projects, beneficiaries):
    """MERGE CohesionProject nodes and beneficiary relationships."""
    total_projects = 0
    total_beneficiaries = 0
    batch = []
    t0 = time.time()

    with driver.session() as session:
        session.run(CONSTRAINT_CYPHER)
        logger.info("Constraint ensured")

        # Load projects
        for project in projects:
            batch.append(project)
            if len(batch) >= BATCH_SIZE:
                session.run(MERGE_PROJECT, batch=batch)
                session.run(LINK_NUTS, batch=batch)
                total_projects += len(batch)
                batch = []
                if total_projects % 10000 < BATCH_SIZE:
                    logger.info("  %d projects loaded", total_projects)

        if batch:
            session.run(MERGE_PROJECT, batch=batch)
            session.run(LINK_NUTS, batch=batch)
            total_projects += len(batch)
            batch = []

        logger.info("Loaded %d projects, linking beneficiaries ...",
                     total_projects)

        # Load beneficiaries
        for beneficiary in beneficiaries:
            batch.append(beneficiary)
            if len(batch) >= BATCH_SIZE:
                session.run(MERGE_BENEFICIARY_EXACT, batch=batch)
                session.run(MERGE_BENEFICIARY_BY_NAME, batch=batch)
                total_beneficiaries += len(batch)
                batch = []
                if total_beneficiaries % 10000 < BATCH_SIZE:
                    logger.info(
                        "  %d beneficiaries processed", total_beneficiaries
                    )

        if batch:
            session.run(MERGE_BENEFICIARY_EXACT, batch=batch)
            session.run(MERGE_BENEFICIARY_BY_NAME, batch=batch)
            total_beneficiaries += len(batch)

    elapsed = time.time() - t0
    return {
        "total_projects": total_projects,
        "total_beneficiaries": total_beneficiaries,
        "elapsed_s": round(elapsed, 1),
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load EU Knowledge Graph cohesion projects into Neo4j"
    )
    parser.add_argument(
        "--file",
        help="Path to a local JSON dump of projects",
    )
    parser.add_argument(
        "--since",
        default="2025-09-01",
        help="Only ingest projects with start_date >= YYYY-MM-DD "
             "(default: 2025-09-01)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max projects to fetch (default: all)",
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
            projects = list(load_projects_from_file(args.file))
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to read file %s", args.file)
            sys.exit(1)
        beneficiaries = []
    else:
        logger.info(
            "Querying SPARQL endpoint (since=%s, limit=%s)",
            args.since, args.limit,
        )
        projects = list(fetch_projects(
            since=args.since, max_projects=args.limit
        ))
        beneficiaries = list(fetch_beneficiaries(
            max_records=args.limit
        ))

    logger.info(
        "Loaded %d projects and %d beneficiaries from source",
        len(projects), len(beneficiaries),
    )

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = load_into_neo4j(driver, projects, beneficiaries)
    finally:
        driver.close()

    logger.info(
        "Done: %d projects, %d beneficiaries in %.1fs",
        summary["total_projects"],
        summary["total_beneficiaries"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
