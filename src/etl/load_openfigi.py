"""
OpenFIGI ISIN-to-Ticker Enrichment
====================================
Reads Listing nodes that have an ISIN but no ticker from Neo4j, queries
the OpenFIGI v3 mapping API in batches, and SETs ticker, exchange_code,
and figi on matching Listing nodes.

Usage:
    python -m src.etl.load_openfigi --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_openfigi --api-key YOUR_KEY --limit 5000
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import httpx
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
API_BATCH_SIZE = 100  # OpenFIGI max per request
NEO4J_BATCH_SIZE = 500
# Rate limit: 25 requests per 6 seconds
RATE_LIMIT_SLEEP = 0.25  # seconds between requests (conservative)


FETCH_ISINS = """
MATCH (l:Listing)
WHERE l.isin IS NOT NULL AND l.ticker IS NULL
RETURN l.isin AS isin
LIMIT $limit
"""

UPDATE_LISTING = """
UNWIND $batch AS row
MATCH (l:Listing {isin: row.isin})
SET l.ticker        = row.ticker,
    l.exchange_code = row.exchange_code,
    l.figi          = row.figi
"""


def fetch_isins(driver, limit):
    """Get ISINs from Listing nodes that lack a ticker."""
    with driver.session() as session:
        result = session.run(FETCH_ISINS, limit=limit)
        return [r["isin"] for r in result]


def query_openfigi(isins, api_key=None):
    """
    Query OpenFIGI API for a batch of ISINs (max 100).

    Returns a list of dicts with isin, ticker, exchange_code, figi.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key

    payload = [{"idType": "ID_ISIN", "idValue": isin} for isin in isins]

    try:
        resp = httpx.post(
            OPENFIGI_URL, json=payload, headers=headers, timeout=30
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
        results.append({
            "isin": isins[i],
            "ticker": best.get("ticker", ""),
            "exchange_code": best.get("exchCode", ""),
            "figi": best.get("figi", ""),
        })
    return results


def load_into_neo4j(driver, enriched):
    """SET ticker/exchange_code/figi on Listing nodes."""
    total = 0
    batch = []

    with driver.session() as session:
        for rec in enriched:
            batch.append(rec)
            if len(batch) >= NEO4J_BATCH_SIZE:
                session.run(UPDATE_LISTING, batch=batch)
                total += len(batch)
                batch = []

        if batch:
            session.run(UPDATE_LISTING, batch=batch)
            total += len(batch)

    return total


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Enrich Listing nodes with OpenFIGI ticker data"
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENFIGI_API_KEY", ""),
        help="OpenFIGI API key (or set OPENFIGI_API_KEY env var)",
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
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )

    try:
        isins = fetch_isins(driver, args.limit)
        logger.info("Found %d Listing nodes without ticker", len(isins))

        if not isins:
            logger.info("Nothing to enrich — all Listing nodes have tickers")
            return

        t0 = time.time()
        all_enriched = []
        api_key = args.api_key or None
        errors = 0

        for i in range(0, len(isins), API_BATCH_SIZE):
            batch_isins = isins[i : i + API_BATCH_SIZE]
            results = query_openfigi(batch_isins, api_key)
            if not results and batch_isins:
                errors += 1
            all_enriched.extend(results)

            if (i + API_BATCH_SIZE) % 1000 < API_BATCH_SIZE:
                logger.info(
                    "  %d / %d ISINs queried, %d enriched so far",
                    min(i + API_BATCH_SIZE, len(isins)),
                    len(isins),
                    len(all_enriched),
                )

            time.sleep(RATE_LIMIT_SLEEP)

        updated = load_into_neo4j(driver, all_enriched)
        elapsed = time.time() - t0

        logger.info(
            "Done: %d ISINs queried, %d enriched, %d updated in Neo4j, "
            "%d API errors in %.1fs",
            len(isins), len(all_enriched), updated, errors, elapsed,
        )
    except httpx.HTTPError:
        logger.exception("Fatal HTTP error during OpenFIGI enrichment")
        sys.exit(1)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
