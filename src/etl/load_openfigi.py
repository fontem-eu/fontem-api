"""
OpenFIGI ISIN-to-Ticker Enrichment → event log
================================================
Reads existing Listing nodes that have an ISIN but no ticker
(typically inserted by FIRDS or another instrument-reference
loader before OpenFIGI confirmed the canonical ticker), queries
the OpenFIGI v3 mapping API in batches, and emits
``UpsertListing`` events for each match. The Virtuoso + Neo4j
sinks pick the events up and project the enriched Listing.

Source for the ISIN list is still Neo4j today — the event log is
canonical for *writes*, but the per-domain consumers (sinks) are
the source for downstream queries. This loader queries Neo4j as
a derived read store; the source-of-truth ISIN/Listing data
originates upstream (FIRDS).

Note: Listings are keyed by ticker. A Listing with ISIN and no
ticker that gets enriched ends up keyed by the new ticker; the
old isin-only Neo4j node is left in place. A separate cleanup
sweep can drop those once OpenFIGI has run a full cycle.

Usage:
    python -m src.etl.load_openfigi --neo4j-uri bolt://neo4j:7687
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid

import httpx
from gmr_event_schemas import builders
from gmr_events import EventLog
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
API_BATCH_SIZE = 100  # OpenFIGI max per request
# Rate limit: 25 requests per 6 seconds
RATE_LIMIT_SLEEP = 0.25  # seconds between requests (conservative)


# Pull the parent Company so the UpsertListing events carry
# company_gmr_id (the schema requires it). LISTED_AS is the
# Company → Listing edge maintained by the sinks.
FETCH_ISINS = """
MATCH (c:Company)-[:LISTED_AS]->(l:Listing)
WHERE l.isin IS NOT NULL AND (l.ticker IS NULL OR l.ticker = '')
RETURN l.isin AS isin, c.gmr_id AS company_gmr_id
LIMIT $limit
"""


def fetch_isins(driver, limit):
    """Get (isin, company_gmr_id) pairs for Listings without a ticker."""
    with driver.session() as session:
        result = session.run(FETCH_ISINS, limit=limit)
        return [
            {"isin": r["isin"], "company_gmr_id": r["company_gmr_id"]}
            for r in result
        ]


def query_openfigi(isins, api_key=None):
    """Query OpenFIGI for a batch of ISINs (max 100). Returns a
    list of dicts keyed back to the input ISINs."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key

    payload = [{"idType": "ID_ISIN", "idValue": isin} for isin in isins]

    try:
        resp = httpx.post(
            OPENFIGI_URL, json=payload, headers=headers, timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("OpenFIGI API request failed")
        return []

    results = []
    for i, entry in enumerate(resp.json()):
        if "data" not in entry or not entry["data"]:
            continue
        best = entry["data"][0]
        ticker = (best.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        results.append({
            "isin": isins[i],
            "ticker": ticker,
            "exchange_code": (best.get("exchCode") or "").strip(),
            "mic": (best.get("micCode") or "").strip() or None,
            "figi": (best.get("figi") or "").strip(),
        })
    return results


def emit_listing_events(log: EventLog, enriched: list[dict]) -> int:
    """Emit one UpsertListing event per enriched ISIN. Returns the
    count emitted."""
    if not enriched:
        return 0
    batch_id = uuid.uuid4()
    total = 0
    with log.batch(batch_id, producer="load_openfigi") as emit:
        for rec in enriched:
            emit.upsert(
                "UpsertListing",
                iri=f"http://data.fontem.eu/id/Listing/{rec['ticker']}",
                domain="listing",
                payload=builders.upsert_listing(
                    ticker=rec["ticker"],
                    company_gmr_id=rec["company_gmr_id"],
                    exchange=rec.get("exchange_code") or None,
                    isin=rec["isin"],
                    mic=rec.get("mic"),
                    active=True,
                ),
            )
            total += 1
    return total


def load_openfigi(driver, log: EventLog, limit: int, api_key: str | None) -> dict:
    """Read ISINs needing enrichment, query OpenFIGI, emit events."""
    rows = fetch_isins(driver, limit)
    logger.info("Found %d Listings with ISIN but no ticker", len(rows))
    if not rows:
        return {"queried": 0, "enriched": 0, "errors": 0, "emitted": 0}

    isins = [r["isin"] for r in rows]
    isin_to_company = {r["isin"]: r["company_gmr_id"] for r in rows}

    t0 = time.time()
    all_enriched: list[dict] = []
    errors = 0

    for i in range(0, len(isins), API_BATCH_SIZE):
        batch_isins = isins[i:i + API_BATCH_SIZE]
        results = query_openfigi(batch_isins, api_key)
        if not results and batch_isins:
            errors += 1
        for r in results:
            r["company_gmr_id"] = isin_to_company[r["isin"]]
        all_enriched.extend(results)

        if (i + API_BATCH_SIZE) % 1000 < API_BATCH_SIZE:
            logger.info(
                "  %d / %d ISINs queried, %d enriched so far",
                min(i + API_BATCH_SIZE, len(isins)), len(isins),
                len(all_enriched),
            )
        time.sleep(RATE_LIMIT_SLEEP)

    emitted = emit_listing_events(log, all_enriched)
    elapsed = time.time() - t0
    logger.info(
        "OpenFIGI: %d ISINs queried, %d enriched, %d events emitted, "
        "%d API errors in %.1fs",
        len(isins), len(all_enriched), emitted, errors, elapsed,
    )
    return {
        "queried": len(isins),
        "enriched": len(all_enriched),
        "emitted": emitted,
        "errors": errors,
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Enrich Listing nodes with OpenFIGI ticker data",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENFIGI_API_KEY", ""),
        help="OpenFIGI API key (or OPENFIGI_API_KEY env var)",
    )
    parser.add_argument(
        "--limit", type=int, default=10000,
        help="Max ISINs to process (default: 10000)",
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
        load_openfigi(driver, log, args.limit, args.api_key or None)
    except httpx.HTTPError:
        logger.exception("Fatal HTTP error during OpenFIGI enrichment")
        sys.exit(1)
    finally:
        driver.close()
        log.close()


if __name__ == "__main__":
    main()
