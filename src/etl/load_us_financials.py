"""
US EDGAR Financials → events.entity_events
==========================================
Reads ``companyfacts/*.json`` and emits one ``UpsertFiling`` event
per (company, year) into the event log. The Filing IRI is
deterministic on (gmr_id, year, source), so re-runs land on the
same subject and the sinks idempotently overwrite.

We used to bracket the stream with ``BeginGraphReplace`` /
``EndGraphReplace``, giving Virtuoso PUT-replace semantics and the
Neo4j sink a DETACH-DELETE/MERGE flow. That pattern collapsed
under its own weight in prod 2026-06-07: the Neo4j sink
accumulates the whole bracket in memory before flushing, 130k
filings ≈ 650 MiB on a 1 Gi pod = OOMKilled mid-bracket, and the
EndGraphReplace landed in a fresh process with no Begin to match,
silently no-op'd. EDGAR + ESEF also share the FinancialYear label,
so even on a clean flush each loader's DETACH would wipe the
other source's data. Per-event MERGE has neither problem.

Trade-off: filings retracted upstream linger in the graph until a
follow-up cleanup pass removes them. For EDGAR retractions this is
rare and tolerable; the next iteration of an ETL refresh job can
prune stale (gmr_id, year, source) rows.

Usage:
    python -m src.etl.load_us_financials --edgar-dir /edgar-data/full
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import uuid
from pathlib import Path

from fontem_event_schemas import builders
from fontem_event_schemas.validate import EventValidationError
from fontem_events import EventLog

logger = logging.getLogger(__name__)

SOURCE = "edgar"

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


# Each `if` branch handles a distinct US-GAAP concept (revenue, op_income,
# net_income, eps, equity, debt, dividends, inventory, ...) with its own
# unit-conversion + fiscal-year-alignment quirk. Splitting would scatter the
# concept-by-concept table across files. Locals are loop variables of the
# single annual-fact aggregation pass.
def _extract_annual(facts_json: dict) -> list[dict]:  # pylint: disable=too-many-locals,too-many-branches
    """Extract annual financial data from a companyfacts JSON."""
    usgaap = facts_json.get("facts", {}).get("us-gaap", {})
    if not usgaap:
        return []

    raw: dict[str, dict[str, float]] = {}
    for field, concepts in _CONCEPT_MAP.items():
        merged: dict[str, float] = {}
        for concept in reversed(concepts):
            data = usgaap.get(concept, {})
            for _unit_key, entries in data.get("units", {}).items():
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

    all_years = set()
    for vals in raw.values():
        all_years.update(vals.keys())

    records = []
    for year_str in sorted(all_years, reverse=True)[:10]:
        rec = {"year": int(year_str)}
        has_data = False
        for field in _CONCEPT_MAP:
            val = raw.get(field, {}).get(year_str)
            if val is not None:
                rec[field] = val
                has_data = True
        if has_data:
            records.append(rec)
    return records


def load_us_financials(  # pylint: disable=too-many-locals
    log: EventLog, edgar_dir: Path,
) -> dict:
    """Iterate companyfacts/*.json and emit one UpsertFiling event
    per (company, year). Each event is independently MERGEable in
    the sinks — no Begin/End bracket.

    Bad rows (year out of schema range, missing mandatory fields, junk
    XBRL values) are logged and skipped per-record — see the comment on
    EventValidationError below. We don't want one corrupt CIK to abort
    the whole 19k-company sweep.

    Returns ``{"total": <filings>, "companies": <distinct CIKs>,
                "skipped_validation": <bad records>}``."""
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

    batch_id = uuid.uuid4()
    companies_loaded = 0
    filings_emitted = 0
    skipped_validation = 0
    t0 = time.time()

    with log.batch(batch_id, producer="load_us_financials") as emit:
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
                year = rec.pop("year")
                try:
                    emit.upsert(
                        "UpsertFiling",
                        iri=(
                            f"http://data.fontem.eu/id/Filing/"
                            f"{_filing_uuid(company_gmr, year, SOURCE)}"
                        ),
                        domain="financials/edgar",
                        payload=builders.upsert_filing(
                            gmr_id=company_gmr,
                            year=year,
                            source=SOURCE,
                            **rec,
                        ),
                    )
                    filings_emitted += 1
                except EventValidationError as exc:
                    # EDGAR companyfacts contains the occasional junk
                    # record (year out of range, NaN values, etc — we
                    # saw 2113 in the wild from a botched 10-K). The
                    # schema rejects them at emit time; without this
                    # try/except a single bad CIK aborts the whole
                    # 19k-company sweep.
                    skipped_validation += 1
                    logger.warning(
                        "skipping bad filing CIK=%s year=%s: %s",
                        cik_str, year, exc,
                    )
            companies_loaded += 1
            if companies_loaded % 500 == 0:
                elapsed = time.time() - t0
                logger.info(
                    "  %d companies, %d filings (%.0f co/s)",
                    companies_loaded, filings_emitted,
                    companies_loaded / elapsed if elapsed else 0,
                )

    elapsed = time.time() - t0
    logger.info(
        "Done: %d filings for %d companies in %.1fs (%d skipped on validation)",
        filings_emitted, companies_loaded, elapsed, skipped_validation,
    )
    return {
        "total": filings_emitted,
        "companies": companies_loaded,
        "skipped_validation": skipped_validation,
    }


def _filing_uuid(gmr_id: str, year: int, source: str) -> uuid.UUID:
    """Deterministic Filing IRI key — matches the Virtuoso sink's
    renderer (gmr-virtuoso-sink/triples.py:render_upsert_filing).
    Re-runs on the same (gmr_id, year, source) land on the same
    IRI so PUT-replace semantics overwrite cleanly."""
    seed = f"filing:{gmr_id}:{year}:{source}"
    return uuid.uuid5(
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), seed,
    )


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertFiling events for US EDGAR financials",
    )
    parser.add_argument(
        "--edgar-dir",
        default=os.environ.get(
            "GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full",
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    log = EventLog.from_env()
    try:
        load_us_financials(log, Path(args.edgar_dir))
    finally:
        log.close()


if __name__ == "__main__":
    main()
