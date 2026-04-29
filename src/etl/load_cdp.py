"""
CDP Open Data Portal → Neo4j Company Enrichment
=================================================
Queries the CDP SODA API for corporate climate disclosure scores and
SETs cdp_score, scope1_emissions, scope2_emissions, and reporting_year
on matching Company nodes.

Company matching is by name + country using the Neo4j full-text index
with SAME_AS for low-confidence fuzzy matches.

Usage:
    python -m src.etl.load_cdp --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_cdp --year 2024 --limit 5000
"""
from __future__ import annotations

import argparse
import logging
import os
import time

import httpx
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# CDP datasets on data.cdp.net (SODA API)
CDP_API_BASE = "https://data.cdp.net/resource"
# Corporate responses dataset — the actual resource ID may change;
# this is a well-known stable identifier for the climate scores.
CDP_DATASET_ID = os.environ.get("CDP_DATASET_ID", "maxh-kwc2")

BATCH_SIZE = 500

# Minimum company-name length for fuzzy CDP matching. CDP rows arrive
# with the legal organisation name, which is rarely shorter than this;
# anything below the floor is suspect.
MIN_NAME_LEN = 6

# Exact match path: same name, same country. Safe — the only reason to
# guard further is if CDP ever ships rows with empty country, which
# would otherwise cross-pollute properties between same-named entities
# in different jurisdictions.
UPDATE_COMPANY_EXACT = """
UNWIND $batch AS row
MATCH (c:Company)
WHERE c.name = row.company_name
  AND c.country = row.country
  AND coalesce(row.country, '') <> ''
SET c.cdp_score        = row.cdp_score,
    c.scope1_emissions = row.scope1_emissions,
    c.scope2_emissions = row.scope2_emissions,
    c.reporting_year   = row.reporting_year
"""

# Fuzzy path REMOVED. Previous version SET cdp_score / emissions on the
# top-scoring candidate from a name fulltext index with score>2.0 and
# a NULL-bypassing country guard — same shape of bug as the sanctions
# matcher. There's no value-side recovery for "wrong Company has
# someone else's CO2 number" since the data is just SET on the node;
# we can't review-queue a property mutation.
#
# When the central /resolve service lands on gmr-consolidator, this
# loader migrates to: POST /resolve with the CDP organisation row;
# only SET properties on a confident match (LEI tier or name+country
# tier). Until then, exact name+country is the only path. CDP rows
# without a clean Company match are skipped — silently miss > silently
# corrupt.

def fetch_cdp_data(year, limit):
    """Query CDP SODA API for climate scores."""
    url = f"{CDP_API_BASE}/{CDP_DATASET_ID}.json"
    params = {
        "$where": f"reporting_year='{year}'",
        "$limit": limit,
        "$order": "organization ASC",
    }
    logger.info("Querying CDP API for year %s (limit %d)...", year, limit)
    try:
        resp = httpx.get(url, params=params, timeout=60)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to query CDP API (dataset %s may require membership)", CDP_DATASET_ID)
        return []

    data = resp.json()
    logger.info("Received %d records from CDP", len(data))

    records = []
    for row in data:
        company_name = (row.get("organization") or "").strip()
        if not company_name:
            continue

        country = (row.get("country") or "").strip()
        cdp_score = (row.get("score") or row.get("cdp_score") or "").strip()

        scope1_raw = row.get("scope_1_emissions") or row.get("scope1") or ""
        scope2_raw = row.get("scope_2_emissions") or row.get("scope2") or ""
        try:
            scope1 = float(scope1_raw) if scope1_raw else None
        except ValueError:
            scope1 = None
        try:
            scope2 = float(scope2_raw) if scope2_raw else None
        except ValueError:
            scope2 = None

        records.append({
            "company_name": company_name,
            "country": country,
            "cdp_score": cdp_score,
            "scope1_emissions": scope1,
            "scope2_emissions": scope2,
            "reporting_year": int(year),
        })

    return records


def load_into_neo4j(driver, records):
    """SET CDP properties on Company nodes that match by exact name+country.

    Fuzzy matching used to live here but was removed — see the
    MATCH_COMPANY_FUZZY comment block above. Migration to /resolve is
    tracked in fix/lobbying-cdp-match-guards.
    """
    exact_updated = 0
    skipped = 0
    batch = []
    t0 = time.time()

    with driver.session() as session:
        for rec in records:
            batch.append(rec)
            if len(batch) >= BATCH_SIZE:
                result = session.run(UPDATE_COMPANY_EXACT, batch=batch)
                exact_updated += result.consume().counters.properties_set // 4
                skipped += len(batch) - result.consume().counters.properties_set // 4
                batch = []

        if batch:
            result = session.run(UPDATE_COMPANY_EXACT, batch=batch)
            exact_updated += result.consume().counters.properties_set // 4

    elapsed = time.time() - t0
    return {
        "total": len(records),
        "exact_updated": exact_updated,
        "elapsed_s": round(elapsed, 1),
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load CDP corporate climate scores into Neo4j"
    )
    parser.add_argument(
        "--year", default="2025",
        help="CDP reporting year (default: 2025)",
    )
    parser.add_argument(
        "--limit", type=int, default=10000,
        help="Max records from SODA API (default: 10000)",
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

    records = fetch_cdp_data(args.year, args.limit)
    if not records:
        logger.info("No CDP records found for year %s", args.year)
        return

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = load_into_neo4j(driver, records)
        from src.etl import _freshness  # pylint: disable=import-outside-toplevel
        _freshness.update_source(
            driver,
            source_id="cdp",
            label="CDP corporate climate scores",
            coverage_start=f"{args.year}-01-01",
            coverage_end=f"{args.year}-12-31",
            record_count=summary.get("total", 0),
            expected_cadence_hours=24 * 100,  # quarterly
        )
    finally:
        driver.close()

    logger.info(
        "Done: %d CDP records, %d exact matches in %.1fs "
        "(fuzzy path removed — migrate to /resolve)",
        summary["total"],
        summary["exact_updated"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
