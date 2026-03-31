"""
French Company Directors → Neo4j
==================================
Fetches director data from the recherche-entreprises.api.gouv.fr API
(free, no API key) and creates Person nodes with [:DIRECTS] relationships.

Matches French companies by name against the API (SIREN resolution).
Rate-limited to ~1 request per 3 seconds to respect the API.

Usage:
    python -m src.etl.load_fr_directors --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_fr_directors --limit 100  # test with 100 companies
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
import uuid

import httpx
from neo4j import GraphDatabase

from . import gmr_id as gmr_id_mod

logger = logging.getLogger(__name__)

API_URL = "https://recherche-entreprises.api.gouv.fr/search"
BATCH_SIZE = 100
RATE_LIMIT_DELAY = 3.0  # seconds between API calls
GMR_NAMESPACE = gmr_id_mod.GMR_NAMESPACE


def _person_id(name: str, first_name: str, birth_year: str) -> str:
    """Generate a deterministic person_id from name + birth year."""
    canonical = f"person:{name.upper().strip()}:{first_name.upper().strip()}:{birth_year or ''}"
    return str(uuid.uuid5(GMR_NAMESPACE, canonical))


def fetch_company_directors(  # pylint: disable=too-many-locals
    company_name: str,
    client: httpx.Client,
) -> list[dict] | None:
    """Query the French API for a company's directors by name."""
    try:
        resp = client.get(
            API_URL,
            params={"q": company_name, "page": 1, "per_page": 1},
            timeout=15,
        )
        if resp.status_code == 429:
            logger.debug("Rate limited, sleeping 10s")
            time.sleep(10)
            resp = client.get(
                API_URL,
                params={"q": company_name, "page": 1, "per_page": 1},
                timeout=15,
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        company = results[0]
        return company.get("dirigeants", [])
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug("API error for %s: %s", company_name, exc)
        return None


def load_directors(driver, limit: int | None = None):  # pylint: disable=too-many-locals
    """Fetch French company directors and load into Neo4j."""
    merge_query = """
    UNWIND $batch AS row
    MERGE (p:Person {person_id: row.person_id})
    SET p.name       = row.name,
        p.first_name = row.first_name,
        p.birth_year = row.birth_year,
        p.nationality = row.nationality,
        p.source     = 'FR_RNE'
    WITH p, row
    MATCH (c:Company {gmr_id: row.gmr_id})
    MERGE (p)-[r:DIRECTS {role: row.role}]->(c)
    SET r.current    = row.current,
        r.start_date = row.start_date,
        r.source     = 'FR_RNE'
    """

    with driver.session() as session:
        # Create constraints
        session.run(
            "CREATE CONSTRAINT person_id IF NOT EXISTS "
            "FOR (p:Person) REQUIRE p.person_id IS UNIQUE"
        )

        # Get French companies to query — prioritize listed + contracted
        query = (
            "MATCH (c:Company {country: 'FR'}) "
            "OPTIONAL MATCH (c)-[:LISTED_AS]->(l:Listing) "
            "OPTIONAL MATCH (ct:Contract)-[:AWARDED_TO]->(c) "
            "WITH c, l, count(ct) AS contracts "
            "ORDER BY CASE WHEN l IS NOT NULL THEN 0 ELSE 1 END, "
            "  contracts DESC "
        )
        if limit:
            query += f"LIMIT {limit} "
        query += "RETURN c.gmr_id AS gmr_id, c.name AS name"

        companies = session.run(query).data()

    logger.info("Fetching directors for %d French companies", len(companies))

    batch = []
    total_persons = 0
    companies_with_data = 0
    t0 = time.time()

    client = httpx.Client()
    try:
        for i, company in enumerate(companies):
            directors = fetch_company_directors(company["name"], client)
            if directors:
                companies_with_data += 1
                for d in directors:
                    if d.get("type_dirigeant") != "personne physique":
                        continue
                    nom = d.get("nom", "").strip()
                    prenoms = d.get("prenoms", "").strip()
                    if not nom:
                        continue
                    batch.append({
                        "person_id": _person_id(
                            nom, prenoms, d.get("annee_de_naissance", ""),
                        ),
                        "name": nom,
                        "first_name": prenoms,
                        "birth_year": d.get("annee_de_naissance"),
                        "nationality": d.get("nationalite"),
                        "role": d.get("qualite", "Dirigeant"),
                        "current": True,
                        "start_date": None,
                        "gmr_id": company["gmr_id"],
                    })

            if len(batch) >= BATCH_SIZE:
                with driver.session() as session:
                    session.run(merge_query, batch=batch)
                total_persons += len(batch)
                batch = []

            if (i + 1) % 50 == 0:
                elapsed = time.time() - t0
                logger.info(
                    "  %d/%d companies queried, %d persons loaded (%.1f co/min)",
                    i + 1, len(companies), total_persons,
                    (i + 1) / (elapsed / 60) if elapsed else 0,
                )

            time.sleep(RATE_LIMIT_DELAY)
    finally:
        client.close()

    if batch:
        with driver.session() as session:
            session.run(merge_query, batch=batch)
        total_persons += len(batch)

    elapsed = time.time() - t0
    logger.info(
        "Done: %d persons from %d companies (of %d queried) in %.0fs",
        total_persons, companies_with_data, len(companies), elapsed,
    )
    return {
        "persons": total_persons,
        "companies_with_data": companies_with_data,
        "companies_queried": len(companies),
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load French company directors into Neo4j",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limit number of companies to query (for testing)",
    )
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "gmr-neo4j-2026"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    driver = GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    try:
        load_directors(driver, limit=args.limit)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
