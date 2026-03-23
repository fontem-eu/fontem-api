"""
Local EDGAR Fetcher
====================
Reads financial facts from the SEC EDGAR bulk data (companyfacts.zip /
submissions.zip) already downloaded to a local directory by edgar-data-fetcher.

Uses edgartools' EntityFacts API instead of XBRLS.from_filings(), which means
no network calls are made for fundamental data.  The ticker-list endpoint also
reads from the local reference/company_tickers.json instead of calling SEC.

Price data (yfinance) is unaffected — it still comes from the network.

Concept matching re-uses the same XBRL priority-name lists from EdgarFetcher,
filtering to only names without spaces (i.e. actual XBRL concept names, not
human-readable labels).  Concepts are looked up as "us-gaap:<ConceptName>".
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from edgar import Company, set_identity, use_local_storage

# Re-use the same XBRL concept name lists — import the private constants and
# filter out any entries that contain spaces (those are display labels).
from .edgar_fetcher import (  # pylint: disable=import-private-name
    _REVENUE,
    _NET_INCOME,
    _TOTAL_ASSETS,
    _TOTAL_LIABILITIES,
    _EQUITY,
    _OPERATING_CF,
    _CURRENT_ASSETS,
    _CURRENT_LIABILITIES,
    _SHARES_OUTSTANDING,
    _EPS,
    _INVENTORY,
    _PREPAID_EXPENSES,
    _CAPEX,
    _GROSS_PROFIT,
    _OPERATING_INCOME,
    _LONG_TERM_DEBT,
    _CASH_AND_EQUIVALENTS,
    _DEPRECIATION_AMORTIZATION,
    _INTEREST_EXPENSE,
    _INCOME_TAX_EXPENSE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Annual report form types we recognise
# ---------------------------------------------------------------------------
_ANNUAL_FORMS = {"10-K", "20-F", "40-F"}


def _xbrl_names(label_list: List[str]) -> List[str]:
    """Return only XBRL concept names (no spaces) from a label priority list."""
    return [lbl for lbl in label_list if " " not in lbl]


# Pre-filtered lists — only XBRL concept names, no human-readable labels
_R_REVENUE          = _xbrl_names(_REVENUE)
_R_NET_INCOME       = _xbrl_names(_NET_INCOME)
_R_TOTAL_ASSETS     = _xbrl_names(_TOTAL_ASSETS)
_R_TOTAL_LIABILITIES= _xbrl_names(_TOTAL_LIABILITIES)
_R_EQUITY           = _xbrl_names(_EQUITY)
_R_OPERATING_CF     = _xbrl_names(_OPERATING_CF)
_R_CURRENT_ASSETS   = _xbrl_names(_CURRENT_ASSETS)
_R_CURRENT_LIABILITIES = _xbrl_names(_CURRENT_LIABILITIES)
_R_SHARES           = _xbrl_names(_SHARES_OUTSTANDING)
_R_EPS              = _xbrl_names(_EPS)
_R_INVENTORY        = _xbrl_names(_INVENTORY)
_R_PREPAID          = _xbrl_names(_PREPAID_EXPENSES)
_R_CAPEX            = _xbrl_names(_CAPEX)
_R_GROSS_PROFIT     = _xbrl_names(_GROSS_PROFIT)
_R_OPERATING_INCOME = _xbrl_names(_OPERATING_INCOME)
_R_LONG_TERM_DEBT   = _xbrl_names(_LONG_TERM_DEBT)
_R_CASH             = _xbrl_names(_CASH_AND_EQUIVALENTS)
_R_DA               = _xbrl_names(_DEPRECIATION_AMORTIZATION)
_R_INTEREST         = _xbrl_names(_INTEREST_EXPENSE)
_R_TAX              = _xbrl_names(_INCOME_TAX_EXPENSE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MIN_ANNUAL_DAYS = 340  # duration facts shorter than this are quarterly/semi-annual


def _build_concept_map(facts) -> tuple[dict, str]:
    """
    Scan all EntityFacts for a company and build a compact map:
        { "us-gaap:ConceptName" → { year(int) → numeric_value } }

    Returns (concept_map, dominant_form_type).

    Important: ``FinancialFact.fiscal_year`` is the *filing* year, not the
    period the data covers.  A 2021 10-K re-states three years of revenue, all
    tagged fiscal_year=2021.  We therefore key on ``period_end.year`` instead.

    Duration facts (income statement / cash flow):
      - Must span ≥ 340 days to qualify as annual (filters quarterly segments).
      - Year key = period_end.year.

    Instant facts (balance sheet):
      - Year key = period_end.year (the measurement date).
      - Later filings overwrite earlier ones for the same concept/year
        (restatements are preferred).

    Facts with missing period dates are skipped — they cannot be placed on the
    correct fiscal year axis.
    """
    concept_map: Dict[str, Dict[int, float]] = {}
    form_counter: Counter = Counter()

    for fact in facts.get_all_facts():
        if fact.fiscal_period != "FY":
            continue
        if fact.numeric_value is None:
            continue

        period_type  = getattr(fact, "period_type",  None)
        period_start = getattr(fact, "period_start", None)
        period_end   = getattr(fact, "period_end",   None)

        if period_end is None:
            continue  # cannot determine year

        try:
            yr = period_end.year
        except AttributeError:
            continue

        if period_type == "duration":
            if period_start is None:
                continue
            try:
                if (period_end - period_start).days < _MIN_ANNUAL_DAYS:
                    continue  # quarterly / semi-annual segment
            except (AttributeError, TypeError):
                continue

        if fact.form_type in _ANNUAL_FORMS:
            form_counter[fact.form_type] += 1

        concept = fact.concept
        if concept not in concept_map:
            concept_map[concept] = {}
        # Overwrite: later-seen value wins — prefers data from more recent
        # filings (which may include restatements of earlier periods).
        concept_map[concept][yr] = fact.numeric_value

    form_type = form_counter.most_common(1)[0][0] if form_counter else "10-K"
    return concept_map, form_type


def _get_annual_series(
    concept_map: dict, xbrl_names: List[str], years: int
) -> pd.Series:
    """
    Merge all matching XBRL concepts and return the most-recent N years as a
    descending Series.  Higher-priority names (earlier in *xbrl_names*) win on
    same-year ties.  Merging is required because companies switch concept names
    over time; first-wins would miss years covered by later-listed names.
    """
    # Merge all matching concepts: iterate low→high priority so that
    # higher-priority names overwrite lower-priority ones for the same year.
    # This handles companies that switch concept names over time
    # (e.g. AAPL used Revenues through 2018, then switched to
    # RevenueFromContractWithCustomerExcludingAssessedTax).
    merged: dict[int, float] = {}
    for name in reversed(xbrl_names):
        key = f"us-gaap:{name}" if ":" not in name else name
        data = concept_map.get(key)
        if data:
            merged.update(data)
    if not merged:
        return pd.Series(dtype=float)
    return pd.Series(merged, dtype=float).sort_index(ascending=False).head(years)


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class LocalEdgarFetcher:
    """
    Fetches fundamental financial data from locally downloaded EDGAR bulk files.

    Reads from:
      {local_data_dir}/companyfacts/CIK{cik}.json  via edgartools EntityFacts
      {local_data_dir}/reference/company_tickers.json  for the ticker list

    No network calls are made by this class.
    """

    def __init__(self, local_data_dir: str) -> None:
        self._local_data_dir = Path(local_data_dir)
        set_identity("local-storage")
        use_local_storage(str(self._local_data_dir))
        logger.info("LocalEdgarFetcher initialised with data dir: %s", self._local_data_dir)

    # ------------------------------------------------------------------
    def fetch_fundamentals(  # pylint: disable=too-many-locals
        self, ticker: str, years: int = 10
    ) -> Dict:
        """
        Return the same dict structure as EdgarFetcher.fetch_fundamentals() but
        sourced entirely from local EntityFacts bulk data.
        """
        logger.info("LocalEdgarFetcher: loading EntityFacts for %s", ticker)
        company = Company(ticker)
        if company is None:
            raise ValueError(f"Unknown ticker '{ticker}'")

        facts = company.get_facts()
        if facts is None:
            raise ValueError(f"No local facts found for '{ticker}'")

        concept_map, form_type = _build_concept_map(facts)
        if not concept_map:
            raise ValueError(f"Empty concept map for '{ticker}' — CIK may not be in local data")

        logger.debug("Concept map has %d concepts for %s", len(concept_map), ticker)

        def _s(names: List[str]) -> pd.Series:
            return _get_annual_series(concept_map, names, years)

        revenue           = _s(_R_REVENUE)
        gross_profit      = _s(_R_GROSS_PROFIT)
        operating_income  = _s(_R_OPERATING_INCOME)
        net_income        = _s(_R_NET_INCOME)
        total_assets      = _s(_R_TOTAL_ASSETS)
        total_liabilities = _s(_R_TOTAL_LIABILITIES)
        equity            = _s(_R_EQUITY)
        long_term_debt    = _s(_R_LONG_TERM_DEBT)
        cash              = _s(_R_CASH)
        operating_cf      = _s(_R_OPERATING_CF)
        da                = _s(_R_DA)
        interest_expense  = _s(_R_INTEREST)
        income_tax        = _s(_R_TAX)
        current_assets    = _s(_R_CURRENT_ASSETS)
        current_liab      = _s(_R_CURRENT_LIABILITIES)
        shares            = _s(_R_SHARES)
        eps               = _s(_R_EPS)
        inventory         = _s(_R_INVENTORY)
        prepaid_expenses  = _s(_R_PREPAID)
        capex             = _s(_R_CAPEX)

        # Warn on missing core concepts (same behaviour as EdgarFetcher)
        if revenue.empty:
            logger.warning("Revenue not found in local facts for %s", ticker)
        if net_income.empty:
            logger.warning("Net income not found in local facts for %s", ticker)

        # Derive equity if not found directly
        if equity.empty and not total_assets.empty and not total_liabilities.empty:
            common = total_assets.index.intersection(total_liabilities.index)
            if len(common):
                equity = total_assets[common] - total_liabilities[common]

        # Free Cash Flow = Operating CF − CapEx
        fcf = pd.Series(dtype=float)
        if not operating_cf.empty and not capex.empty:
            common = operating_cf.index.intersection(capex.index)
            if len(common):
                fcf = operating_cf[common] - capex[common]

        return {
            "ticker":                    ticker.upper(),
            "form_type":                 form_type,
            "revenue":                   revenue,
            "gross_profit":              gross_profit,
            "operating_income":          operating_income,
            "net_income":                net_income,
            "total_assets":              total_assets,
            "total_liabilities":         total_liabilities,
            "equity":                    equity,
            "long_term_debt":            long_term_debt,
            "cash_and_equivalents":      cash,
            "depreciation_amortization": da,
            "interest_expense":          interest_expense,
            "income_tax_expense":        income_tax,
            "operating_cashflow":        operating_cf,
            "capex":                     capex,
            "free_cashflow":             fcf,
            "current_assets":            current_assets,
            "current_liabilities":       current_liab,
            "inventory":                 inventory,
            "prepaid_expenses":          prepaid_expenses,
            "shares_outstanding":        shares,
            "eps":                       eps,
        }

    # ------------------------------------------------------------------
    def get_edgar_ticker_list(self) -> List[Dict]:
        """
        Return the company ticker list from the locally downloaded reference data.
        Reads reference/company_tickers.json — no network call.
        """
        ticker_file = self._local_data_dir / "reference" / "company_tickers.json"
        logger.info("Loading ticker list from local file: %s", ticker_file)

        with open(ticker_file, encoding="utf-8") as fh:
            data = json.load(fh)

        tickers = []
        for _idx, company_info in data.items():
            if not isinstance(company_info, dict):
                continue
            ticker = company_info.get("ticker")
            if not ticker:
                continue

            raw_cik = company_info.get("cik_str", "")
            cik_padded = str(raw_cik).zfill(10)

            ticker_info: Dict = {
                "symbol":      ticker.upper(),
                "cik":         cik_padded,
                "name":        company_info.get("title", "").strip(),
                "sic":         "",
                "sic_description": "Unknown",
                "exchange":    "Unknown",
                "sector":      "Unknown",
                "industry":    "Unknown",
                "country":     "US",
                "currency":    "USD",
                "is_active":   True,
                "last_updated": "",
            }
            ticker_info["search_name"] = (
                f"{ticker_info['name']} {ticker_info['symbol']}".lower()
            )
            ticker_info["search_keywords"] = ticker_info["search_name"]
            tickers.append(ticker_info)

        logger.info("Loaded %d tickers from local reference data", len(tickers))
        return tickers
