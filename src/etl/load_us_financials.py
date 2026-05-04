"""
US EDGAR Financials → Virtuoso fontem:Filing
==============================================
Reads companyfacts/*.json directly (no edgartools dependency)
and emits SHACL-validated fontem:Filing triples into the
``http://data.fontem.eu/graph/financials/edgar`` named graph.

Usage:
    python -m src.etl.load_us_financials --edgar-dir /edgar-data/full \\
        --virtuoso-sparql-endpoint http://virtuoso.gmr.svc.cluster.local:8890/sparql
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .rdf_filings_writer import RdfFilingsWriter

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
    writer: RdfFilingsWriter, edgar_dir: Path,
):
    """Iterate companyfacts/*.json and PUT a fontem:Filing batch
    per ``BATCH_SIZE`` companies into the EDGAR named graph.

    The writer's PUT semantics replace the named graph; we
    therefore accumulate the entire run into a single in-memory
    batch and flush at the end. For the production EDGAR set
    (~10k US-listed companies, ~10 years each → ~100k filings)
    that's ~250 MiB of intermediate Turtle, which fits in the
    cronjob's 2 GiB memory limit comfortably.
    """
    facts_dir = edgar_dir / "companyfacts"
    tickers_path = edgar_dir / "reference" / "company_tickers.json"

    with open(tickers_path, encoding="utf-8") as f:
        tickers_data = json.load(f)

    from . import gmr_id  # pylint: disable=import-outside-toplevel
    cik_to_gmr: dict[str, str] = {}
    for _idx, info in tickers_data.items():
        cik_raw = info.get("cik_str", "")
        if not cik_raw:
            continue
        cik = str(cik_raw).zfill(10)
        cik_to_gmr[cik] = gmr_id.from_cik(cik)

    logger.info("Indexed %d CIK→gmr_id mappings", len(cik_to_gmr))

    all_records: list[dict] = []
    companies_loaded = 0
    t0 = time.time()

    for filename in sorted(facts_dir.glob("CIK*.json")):
        cik_str = filename.stem.replace("CIK", "").lstrip("0").zfill(10)
        company_gmr = cik_to_gmr.get(cik_str)
        if not company_gmr:
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
            rec["gmr_id"] = company_gmr
            all_records.append(rec)
        companies_loaded += 1
        if companies_loaded % 500 == 0:
            elapsed = time.time() - t0
            logger.info(
                "  %d companies, %d fin. years (%.0f co/s)",
                companies_loaded, len(all_records),
                companies_loaded / elapsed if elapsed else 0,
            )

    if not all_records:
        logger.warning("no EDGAR records found; skipping write")
        return {"total": 0, "companies": 0}

    res = writer.write(all_records)
    elapsed = time.time() - t0
    logger.info(
        "Done: %d filings (%d triples) for %d companies in %.1fs",
        res.written, res.triples_pushed, companies_loaded, elapsed,
    )
    return {
        "total": res.written, "companies": companies_loaded,
        "triples": res.triples_pushed,
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load US EDGAR financials into Virtuoso",
    )
    parser.add_argument(
        "--edgar-dir",
        default=os.environ.get("GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full"),
    )
    parser.add_argument(
        "--virtuoso-sparql-endpoint",
        default=os.environ.get("VIRTUOSO_SPARQL_ENDPOINT", ""),
    )
    parser.add_argument(
        "--virtuoso-dba-user",
        default=os.environ.get("VIRTUOSO_DBA_USER", "dba"),
    )
    parser.add_argument(
        "--virtuoso-dba-password",
        default=os.environ.get("VIRTUOSO_DBA_PASSWORD", ""),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.virtuoso_sparql_endpoint:
        logger.error(
            "VIRTUOSO_SPARQL_ENDPOINT must be set; the Neo4j path "
            "was retired by the FinancialYear cutover."
        )
        sys.exit(2)

    writer = RdfFilingsWriter(
        source="edgar",
        sparql_endpoint=args.virtuoso_sparql_endpoint,
        dba_user=args.virtuoso_dba_user,
        dba_password=args.virtuoso_dba_password,
    )
    load_us_financials(writer, Path(args.edgar_dir))


if __name__ == "__main__":
    main()
