"""
CDP Open Data Portal → event log
==================================
Queries the CDP SODA API for corporate climate disclosure scores
and emits one ``UpsertDisclosure`` event per row whose Company
matches a Neo4j Company exactly by name+country. Sinks project
the disclosures into both stores.

Company resolution stays exact-name-and-country (matches the
prior shape). Fuzzy matching was previously removed and remains
out of scope here — a CDP row that doesn't cleanly resolve is
silently skipped (silently miss > silently corrupt). The full
/resolve service migration is tracked separately.

Usage:
    python -m src.etl.load_cdp --year 2025
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import time
import uuid

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

from src.etl._http import HTTP_HEADERS

logger = logging.getLogger(__name__)

# CDP datasets on data.cdp.net (SODA API)
CDP_API_BASE = "https://data.cdp.net/resource"
# Corporate responses dataset — the actual resource ID may change;
# this is a well-known stable identifier for the climate scores.
CDP_DATASET_ID = os.environ.get("CDP_DATASET_ID", "maxh-kwc2")

# Minimum company-name length for fuzzy CDP matching. CDP rows arrive
# with the legal organisation name, which is rarely shorter than this;
# anything below the floor is suspect.
MIN_NAME_LEN = 6

# Resolve a Company's gmr_id by exact name+country. Empty country
# short-circuits — same-named companies in different jurisdictions
# must NOT cross-pollute disclosure attribution.
RESOLVE_COMPANY = """
UNWIND $batch AS row
MATCH (c:Company)
WHERE c.name = row.company_name
  AND c.country = row.country
  AND coalesce(row.country, '') <> ''
RETURN row.company_name AS company_name,
       row.country AS country,
       c.gmr_id AS gmr_id
"""


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
        resp = httpx.get(url, params=params, timeout=60,
                         headers=HTTP_HEADERS)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception(
            "Failed to query CDP API (dataset %s may require membership)",
            CDP_DATASET_ID,
        )
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


def resolve_companies(driver, records):
    """Look up gmr_id for each (company_name, country) pair via the
    derived Neo4j store. Empty country → skipped at the Cypher
    level (the WHERE clause guards). Returns a map keyed by
    (company_name, country)."""
    if not records:
        return {}
    batch = [
        {"company_name": r["company_name"], "country": r["country"]}
        for r in records
    ]
    out: dict[tuple[str, str], str] = {}
    with driver.session() as session:
        for row in session.run(RESOLVE_COMPANY, batch=batch):
            out[(row["company_name"], row["country"])] = row["gmr_id"]
    return out


def _disclosure_id(rec: dict) -> str:
    """Deterministic ID per (year, company_name, country). CDP's
    own SODA API doesn't carry a stable submission ID we can rely
    on across rebuilds, so we hash the natural key."""
    seed = (
        f"cdp:{rec['reporting_year']}:"
        f"{rec['company_name']}:{rec['country']}"
    )
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def emit_disclosure_events(
    log: EventLog, records: list[dict], company_index: dict[tuple[str, str], str],
) -> dict:
    """Emit one UpsertDisclosure event per record that resolved to
    a Company. Returns counters for total / emitted / skipped."""
    if not records:
        return {"total": 0, "emitted": 0, "skipped": 0}

    batch_id = uuid.uuid4()
    emitted = 0
    skipped = 0
    with log.batch(batch_id, producer="load_cdp") as emit:
        for rec in records:
            key = (rec["company_name"], rec["country"])
            gmr_id = company_index.get(key)
            if not gmr_id:
                skipped += 1
                continue
            d_id = _disclosure_id(rec)
            details: dict[str, object] = {}
            if rec.get("cdp_score"):
                details["cdp_score"] = rec["cdp_score"]
            if rec.get("scope1_emissions") is not None:
                details["scope1_emissions"] = rec["scope1_emissions"]
            if rec.get("scope2_emissions") is not None:
                details["scope2_emissions"] = rec["scope2_emissions"]
            emit.upsert(
                "UpsertDisclosure",
                iri=f"http://data.fontem.eu/id/Disclosure/cdp/{d_id}",
                domain="disclosure",
                payload=builders.upsert_disclosure(
                    system="cdp",
                    disclosure_id=d_id,
                    company_gmr_id=gmr_id,
                    disclosure_type="climate-change",
                    year=rec["reporting_year"],
                    title=f"CDP climate disclosure ({rec['reporting_year']})",
                    details=details or None,
                ),
            )
            emitted += 1
    return {"total": len(records), "emitted": emitted, "skipped": skipped}


def load_cdp(driver, log: EventLog, year, limit) -> dict:
    """Top-level orchestration: fetch CDP API → resolve to gmr_ids
    via Neo4j → emit UpsertDisclosure events."""
    t0 = time.time()
    records = fetch_cdp_data(year, limit)
    if not records:
        logger.info("No CDP records found for year %s", year)
        return {"total": 0, "emitted": 0, "skipped": 0, "elapsed_s": 0.0}

    company_index = resolve_companies(driver, records)
    summary = emit_disclosure_events(log, records, company_index)
    summary["elapsed_s"] = round(time.time() - t0, 1)
    logger.info(
        "CDP: %d rows, %d events emitted, %d skipped (no Company match) "
        "in %.1fs",
        summary["total"], summary["emitted"], summary["skipped"],
        summary["elapsed_s"],
    )
    return summary


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load CDP corporate climate scores into the event log",
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

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password),
    )
    log = EventLog.from_env()
    try:
        load_cdp(driver, log, args.year, args.limit)
    finally:
        driver.close()
        log.close()


if __name__ == "__main__":
    main()
