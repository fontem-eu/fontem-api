"""
US EDGAR Financials → Neo4j FinancialYear Nodes
=================================================
Reads companyfacts/*.json directly (no edgartools dependency) and creates
FinancialYear nodes for US companies already in the graph.

Usage:
    python -m src.etl.load_us_financials --edgar-dir /edgar-data/full
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
_ANNUAL_FORMS = {"10-K", "20-F", "40-F"}

# XBRL concept priority lists (first match wins per year)
_CONCEPT_MAP = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues", "SalesRevenueNet",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity"],
    "eps": ["EarningsPerShareBasic", "EarningsPerShareDiluted"],
    "operating_cashflow": [
        "NetCashProvidedByUsedInOperatingActivities",
    ],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
    "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "cash_and_equivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsAndShortTermInvestments",
    ],
    "interest_expense": ["InterestExpense", "InterestAndDebtExpense"],
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
    ],
    "inventory": ["InventoryNet"],
}


def _extract_annual(facts_json: dict) -> list[dict]:
    """Extract annual financial data from a companyfacts JSON."""
    usgaap = facts_json.get("facts", {}).get("us-gaap", {})
    if not usgaap:
        return []

    # For each concept, collect annual values keyed by fiscal year end
    raw: dict[str, dict[str, float]] = {}
    for field, concepts in _CONCEPT_MAP.items():
        merged: dict[str, float] = {}
        for concept in reversed(concepts):
            data = usgaap.get(concept, {})
            for unit_key, entries in data.get("units", {}).items():
                for e in entries:
                    if e.get("form") not in _ANNUAL_FORMS:
                        continue
                    if e.get("fp") != "FY":
                        continue
                    end = e.get("end", "")
                    val = e.get("val")
                    if end and val is not None:
                        year = end[:4]
                        merged[year] = float(val)
        if merged:
            raw[field] = merged

    if not raw:
        return []

    # Collect all years
    all_years = set()
    for vals in raw.values():
        all_years.update(vals.keys())

    # Build one record per year
    records = []
    for year_str in sorted(all_years, reverse=True)[:10]:
        rec = {"year": int(year_str)}
        has_data = False
        for field in _CONCEPT_MAP:
            val = raw.get(field, {}).get(year_str)
            rec[field] = val
            if val is not None:
                has_data = True
        if has_data:
            records.append(rec)
    return records


def load_us_financials(  # pylint: disable=too-many-locals
    driver, edgar_dir: Path,
):
    """Read companyfacts JSON files and create FinancialYear nodes."""
    facts_dir = edgar_dir / "companyfacts"
    tickers_path = edgar_dir / "reference" / "company_tickers.json"

    # Build CIK → ticker/gmr_id mapping from company_tickers.json
    with open(tickers_path, encoding="utf-8") as f:
        tickers_data = json.load(f)

    from . import gmr_id  # pylint: disable=import-outside-toplevel
    cik_to_info: dict[str, dict] = {}
    for _idx, info in tickers_data.items():
        cik_raw = info.get("cik_str", "")
        if not cik_raw:
            continue
        cik = str(cik_raw).zfill(10)
        cik_to_info[cik] = {
            "gmr_id": gmr_id.from_cik(cik),
            "ticker": info.get("ticker", "").upper(),
        }

    logger.info("Indexed %d CIK→gmr_id mappings", len(cik_to_info))

    query = """
    UNWIND $batch AS row
    MATCH (c:Company {gmr_id: row.gmr_id})
    MERGE (f:FinancialYear {gmr_id: row.gmr_id, year: row.year})
    SET f.source                    = 'EDGAR',
        f.revenue                   = row.revenue,
        f.gross_profit              = row.gross_profit,
        f.operating_income          = row.operating_income,
        f.net_income                = row.net_income,
        f.eps                       = row.eps,
        f.total_assets              = row.total_assets,
        f.total_liabilities         = row.total_liabilities,
        f.equity                    = row.equity,
        f.cash                      = row.cash_and_equivalents,
        f.capex                     = row.capex,
        f.operating_cashflow        = row.operating_cashflow,
        f.current_assets            = row.current_assets,
        f.current_liabilities       = row.current_liabilities,
        f.shares_outstanding        = row.shares_outstanding,
        f.long_term_debt            = row.long_term_debt,
        f.interest_expense          = row.interest_expense,
        f.income_tax_expense        = row.income_tax_expense,
        f.depreciation_amortization = row.depreciation_amortization,
        f.inventory                 = row.inventory
    MERGE (c)-[:REPORTED {year: row.year}]->(f)
    """

    batch = []
    total = 0
    companies_loaded = 0
    t0 = time.time()

    with driver.session() as session:
        for filename in sorted(facts_dir.glob("CIK*.json")):
            cik_str = filename.stem.replace("CIK", "").lstrip("0").zfill(10)
            info = cik_to_info.get(cik_str)
            if not info:
                continue

            try:
                with open(filename, encoding="utf-8") as f:
                    facts = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            records = _extract_annual(facts)
            if not records:
                continue

            for rec in records:
                rec["gmr_id"] = info["gmr_id"]
                batch.append(rec)

            companies_loaded += 1

            if len(batch) >= BATCH_SIZE:
                session.run(query, batch=batch)
                total += len(batch)
                batch = []
                if companies_loaded % 500 == 0:
                    elapsed = time.time() - t0
                    logger.info(
                        "  %d companies, %d fin. years (%.0f co/s)",
                        companies_loaded, total,
                        companies_loaded / elapsed if elapsed else 0,
                    )

        if batch:
            session.run(query, batch=batch)
            total += len(batch)

    elapsed = time.time() - t0
    logger.info(
        "Done: %d financial years for %d companies in %.1fs",
        total, companies_loaded, elapsed,
    )
    return {"total": total, "companies": companies_loaded}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load US EDGAR financials into Neo4j",
    )
    parser.add_argument(
        "--edgar-dir",
        default=os.environ.get("GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full"),
    )
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "gmr-neo4j-2026"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    driver = GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    try:
        load_us_financials(driver, Path(args.edgar_dir))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
