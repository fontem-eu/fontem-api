"""
US Companies & Listings → Neo4j
================================
Reads company_tickers.json from the EDGAR data directory and creates
Company + Listing nodes with LISTED_AS relationships.

Financials are NOT bulk-loaded here; the GraphDataSource delegates
EDGAR financials to LocalEdgarFetcher at query time.

Usage:
    python -m src.etl.load_us_companies --edgar-dir /edgar-data/full
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

from neo4j import GraphDatabase

from . import gmr_id

logger = logging.getLogger(__name__)

BATCH_SIZE = 2000


def load_us_companies(driver, tickers_data: dict):
    """Create Company + Listing nodes for each EDGAR ticker."""
    query = """
    UNWIND $batch AS row
    MERGE (c:Company {gmr_id: row.gmr_id})
    SET c.cik     = row.cik,
        c.name    = row.name,
        c.country = 'US',
        c.active  = true
    MERGE (l:Listing {ticker: row.ticker})
    SET l.exchange = 'US',
        l.currency = 'USD',
        l.active   = true
    MERGE (c)-[:LISTED_AS]->(l)
    """

    batch = []
    total = 0
    t0 = time.time()

    with driver.session() as session:
        session.run(
            "CREATE INDEX company_cik IF NOT EXISTS "
            "FOR (c:Company) ON (c.cik)"
        )
        for _idx, info in tickers_data.items():
            ticker = info.get("ticker", "")
            cik_raw = info.get("cik_str", "")
            if not ticker or not cik_raw:
                continue
            cik = str(cik_raw).zfill(10)
            batch.append({
                "gmr_id": gmr_id.from_cik(cik),
                "cik": cik,
                "name": info.get("title", "").strip(),
                "ticker": ticker.upper(),
            })
            if len(batch) >= BATCH_SIZE:
                session.run(query, batch=batch)
                total += len(batch)
                batch = []
                if total % 5000 < BATCH_SIZE:
                    logger.info("  %d companies loaded", total)
        if batch:
            session.run(query, batch=batch)
            total += len(batch)

    elapsed = time.time() - t0
    logger.info("US companies: %d loaded in %.1fs", total, elapsed)
    return total


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load US companies and listings into Neo4j",
    )
    parser.add_argument(
        "--edgar-dir",
        default=os.environ.get(
            "GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full"
        ),
    )
    parser.add_argument("--neo4j-uri", default="bolt://neo4j:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="gmr-neo4j-2026")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    tickers_path = (
        Path(args.edgar_dir) / "reference" / "company_tickers.json"
    )
    if not tickers_path.exists():
        logger.error(
            "company_tickers.json not found at %s", tickers_path
        )
        return

    data = json.loads(tickers_path.read_text(encoding="utf-8"))
    logger.info("Loaded %d US tickers from %s", len(data), tickers_path)

    driver = GraphDatabase.driver(
        args.neo4j_uri,
        auth=(args.neo4j_user, args.neo4j_password),
    )
    try:
        load_us_companies(driver, data)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
