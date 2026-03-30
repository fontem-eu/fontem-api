"""
EU Listings & Financials → Neo4j
=================================
Reads eu_entities.json and summaries/*.json from the ESEF data directory,
creates Listing and FinancialYear nodes, and wires LISTED_AS / REPORTED
relationships to existing Company nodes.

Usage:
    python -m src.etl.load_eu_listings --esef-dir /esef-data/esef
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

BATCH_SIZE = 500

# Simplified country → currency mapping for major EU/EEA markets
COUNTRY_CURRENCY = {
    "AT": "EUR", "BE": "EUR", "CY": "EUR", "DE": "EUR",
    "EE": "EUR", "ES": "EUR", "FI": "EUR", "FR": "EUR",
    "GR": "EUR", "IE": "EUR", "IT": "EUR", "LT": "EUR",
    "LU": "EUR", "LV": "EUR", "MT": "EUR", "NL": "EUR",
    "PT": "EUR", "SI": "EUR", "SK": "EUR", "HR": "EUR",
    "GB": "GBP", "CH": "CHF", "SE": "SEK", "DK": "DKK",
    "NO": "NOK", "PL": "PLN", "CZ": "CZK", "HU": "HUF",
    "RO": "RON", "BG": "BGN", "IS": "ISK", "UA": "UAH",
    "LI": "CHF",
}


def load_listings(driver, entities: dict):
    """Create Listing nodes and LISTED_AS relationships."""
    query = """
    UNWIND $batch AS row
    MERGE (c:Company {gmr_id: row.gmr_id})
    ON CREATE SET c.lei = row.lei, c.name = row.name,
                  c.country = row.country, c.active = true
    MERGE (l:Listing {ticker: row.ticker})
    SET l.exchange = row.exchange,
        l.currency = row.currency,
        l.active   = true
    MERGE (c)-[:LISTED_AS]->(l)
    """
    batch = []
    total = 0

    with driver.session() as session:
        session.run(
            "CREATE CONSTRAINT listing_ticker IF NOT EXISTS "
            "FOR (l:Listing) REQUIRE l.ticker IS UNIQUE"
        )
        for _ticker, meta in entities.items():
            lei = meta.get("lei", "")
            gid = gmr_id.from_lei(lei) if len(lei) == 20 else (
                gmr_id.from_name(
                    meta.get("country", "XX"),
                    meta.get("name", _ticker),
                )
            )
            batch.append({
                "gmr_id": gid,
                "lei": lei if len(lei) == 20 else None,
                "name": meta.get("name", ""),
                "country": meta.get("country", ""),
                "ticker": meta.get("ticker", _ticker),
                "exchange": meta.get("exchange", ""),
                "currency": COUNTRY_CURRENCY.get(
                    meta.get("country", ""), "EUR"
                ),
            })
            if len(batch) >= BATCH_SIZE:
                session.run(query, batch=batch)
                total += len(batch)
                batch = []
        if batch:
            session.run(query, batch=batch)
            total += len(batch)

    logger.info("Listings: %d created/updated", total)
    return total


def load_financials(driver, summaries_dir: Path):
    """Read summary JSON files and create FinancialYear nodes."""
    query = """
    UNWIND $batch AS row
    MATCH (l:Listing {ticker: row.ticker})
    MATCH (c:Company)-[:LISTED_AS]->(l)
    MERGE (f:FinancialYear {gmr_id: c.gmr_id, year: row.year})
    SET f.source             = 'ESEF',
        f.revenue            = row.revenue,
        f.gross_profit       = row.gross_profit,
        f.operating_income   = row.operating_income,
        f.net_income         = row.net_income,
        f.eps                = row.eps,
        f.total_assets       = row.total_assets,
        f.equity             = row.equity,
        f.cash               = row.cash_and_equivalents,
        f.capex              = row.capex,
        f.filing_date        = row.filing_date,
        f.operating_cashflow = row.operating_cashflow,
        f.free_cashflow      = row.free_cashflow,
        f.total_liabilities  = row.total_liabilities,
        f.current_assets     = row.current_assets,
        f.current_liabilities = row.current_liabilities,
        f.inventory          = row.inventory,
        f.shares_outstanding = row.shares_outstanding,
        f.long_term_debt     = row.long_term_debt,
        f.interest_expense   = row.interest_expense,
        f.income_tax_expense = row.income_tax_expense,
        f.depreciation_amortization = row.depreciation_amortization
    MERGE (c)-[:REPORTED {year: row.year}]->(f)
    """

    if not summaries_dir.exists():
        logger.warning("Summaries dir not found: %s", summaries_dir)
        return 0

    batch = []
    total_filings = 0
    files_processed = 0

    with driver.session() as session:
        for path in sorted(summaries_dir.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping %s: %s", path.name, exc)
                continue

            ticker = doc.get("ticker", path.stem)
            for filing in doc.get("filings", []):
                year = filing.get("year")
                if year is None:
                    continue
                row = {"ticker": ticker, "year": int(year)}
                for key in (
                    "filing_date", "revenue", "gross_profit",
                    "operating_income", "net_income", "eps",
                    "total_assets", "equity", "cash_and_equivalents",
                    "capex", "operating_cashflow", "free_cashflow",
                    "total_liabilities", "current_assets",
                    "current_liabilities", "inventory",
                    "shares_outstanding", "long_term_debt",
                    "interest_expense", "income_tax_expense",
                    "depreciation_amortization",
                ):
                    row[key] = filing.get(key)
                batch.append(row)

                if len(batch) >= BATCH_SIZE:
                    session.run(query, batch=batch)
                    total_filings += len(batch)
                    batch = []

            files_processed += 1
            if files_processed % 1000 == 0:
                logger.info(
                    "  %d files, %d filings",
                    files_processed, total_filings,
                )

        if batch:
            session.run(query, batch=batch)
            total_filings += len(batch)

    logger.info(
        "Financials: %d filings from %d files",
        total_filings, files_processed,
    )
    return total_filings


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load EU listings and financials into Neo4j",
    )
    parser.add_argument(
        "--esef-dir",
        default=os.environ.get("GMR_ESEF_DATA_DIR", "/esef-data/esef"),
    )
    parser.add_argument("--neo4j-uri", default="bolt://neo4j:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="gmr-neo4j-2026")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    esef_dir = Path(args.esef_dir)
    entities_path = esef_dir / "eu_entities.json"
    if not entities_path.exists():
        logger.error("eu_entities.json not found at %s", entities_path)
        return

    entities = json.loads(entities_path.read_text(encoding="utf-8"))
    logger.info("Loaded %d EU entities from %s", len(entities), entities_path)

    driver = GraphDatabase.driver(
        args.neo4j_uri,
        auth=(args.neo4j_user, args.neo4j_password),
    )
    t0 = time.time()
    try:
        load_listings(driver, entities)
        load_financials(driver, esef_dir / "summaries")
    finally:
        driver.close()

    logger.info("EU load complete in %.1fs", time.time() - t0)


if __name__ == "__main__":
    main()
