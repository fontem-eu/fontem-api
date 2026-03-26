"""
ESEF Data Source
================
Concrete implementation of FinancialDataSource backed by locally stored
ESEF filing summaries written by esef-data-fetcher.

Reads from:
  {esef_data_dir}/summaries/{TICKER}.json   — per-entity financial summaries
  {esef_data_dir}/eu_entities.json          — entity registry for search/list

Price data is not available for ESEF entities; all price-related methods
return empty/stub values.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pandas as pd

from ...analysis.gmr_data_source import FinancialDataSource

logger = logging.getLogger(__name__)

_FUNDAMENTALS_KEYS = [
    "revenue",
    "net_income",
    "total_assets",
    "total_liabilities",
    "equity",
    "operating_cashflow",
    "capex",
    "free_cashflow",
    "current_assets",
    "current_liabilities",
    "inventory",
    "prepaid_expenses",
    "shares_outstanding",
    "eps",
    "long_term_debt",
    "cash_and_equivalents",
    "depreciation_amortization",
    "interest_expense",
    "income_tax_expense",
]

_DEFAULT_ESEF_DATA_DIR = os.environ.get("GMR_ESEF_DATA_DIR", "/esef-data/esef")


class EsefDataSource(FinancialDataSource):
    """
    Production data source that reads ESEF financial summaries from local files
    produced by esef-data-fetcher.

    Parameters
    ----------
    esef_data_dir:
        Path to the esef output directory.  Defaults to the ``GMR_ESEF_DATA_DIR``
        environment variable, falling back to ``/esef-data/esef``.
    """

    def __init__(self, esef_data_dir: str | None = None) -> None:
        self._data_dir = Path(esef_data_dir or _DEFAULT_ESEF_DATA_DIR)
        logger.info("EsefDataSource: data dir = %s", self._data_dir)
        self._registry_cache: dict | None = None

    # ------------------------------------------------------------------
    # FinancialDataSource interface
    # ------------------------------------------------------------------

    def get_annual_fundamentals(self, ticker: str, years: int = 10) -> dict:
        """
        Read the ESEF summary file for *ticker* and return a dict of
        ``pd.Series`` indexed by integer fiscal year (descending).

        All 19 standard keys are present; missing values are ``None``.
        """
        summary_path = self._data_dir / "summaries" / f"{ticker.replace('/', '_')}.json"
        if not summary_path.exists():
            logger.debug("No ESEF summary file for %s at %s", ticker, summary_path)
            return {key: pd.Series(dtype=float) for key in _FUNDAMENTALS_KEYS}

        doc = json.loads(summary_path.read_text(encoding="utf-8"))
        filings: list[dict] = doc.get("filings", [])

        # Limit to the requested number of years (filings already sorted newest-first)
        filings = filings[:years]

        data: dict[str, dict[int, float | None]] = {key: {} for key in _FUNDAMENTALS_KEYS}
        for filing in filings:
            year = filing.get("year")
            if year is None:
                continue
            for key in _FUNDAMENTALS_KEYS:
                data[key][year] = filing.get(key)

        result: dict[str, pd.Series] = {}
        for key in _FUNDAMENTALS_KEYS:
            if data[key]:
                series = pd.Series(data[key], dtype=object)
                series.index = series.index.astype(int)
                series = series.sort_index(ascending=False)
                result[key] = series
            else:
                result[key] = pd.Series(dtype=float)

        return result

    def get_annual_avg_prices(self, ticker: str, years: int = 10) -> pd.Series:
        """Price data not available for ESEF entities — returns empty Series."""
        return pd.Series(dtype=float)

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        """Dividend data not available for ESEF entities — returns empty Series."""
        return pd.Series(dtype=float)

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        """Price history not available for ESEF entities — returns empty DataFrame."""
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    def get_market_snapshot(self, ticker: str) -> dict:
        """
        Market snapshot stub for ESEF entities.
        All price-dependent fields are ``None``; non-price fields are populated
        from the entity registry where available.
        """
        return {
            "current_price": None,
            "avg_volume": None,
            "shares_outstanding": None,
            "last_dividend": {"date": None, "amount": None},
            "splits": pd.Series(dtype=float),
            "latest_quarter": {},
        }

    # ------------------------------------------------------------------
    # Ticker discovery
    # ------------------------------------------------------------------

    def get_available_tickers(self) -> list[dict]:
        """Return all ESEF entities from eu_entities.json."""
        registry = self._load_registry()
        return list(registry.values())

    def search_tickers(self, query: str, limit: int = 10) -> list[dict]:
        """Search ESEF tickers by name or symbol (case-insensitive)."""
        all_tickers = self.get_available_tickers()
        if not query:
            return all_tickers[:limit]
        query_lower = query.lower()
        matches = []
        for ticker in all_tickers:
            if (
                query_lower in ticker.get("search_name", "")
                or query_lower in ticker.get("symbol", "").lower()
                or query_lower in ticker.get("search_keywords", "")
            ):
                matches.append(ticker)
                if len(matches) >= limit:
                    break
        return matches

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_registry(self) -> dict:
        if self._registry_cache is not None:
            return self._registry_cache

        registry_path = self._data_dir / "eu_entities.json"
        if not registry_path.exists():
            logger.warning("ESEF registry not found at %s", registry_path)
            self._registry_cache = {}
            return self._registry_cache

        raw: dict = json.loads(registry_path.read_text(encoding="utf-8"))

        # Enrich each entry with search fields expected by search_tickers()
        for ticker, meta in raw.items():
            name = meta.get("name", "")
            symbol = meta.get("symbol", ticker)
            meta.setdefault("ticker", ticker)
            meta["search_name"] = f"{name} {symbol}".lower()
            meta["search_keywords"] = meta["search_name"]
            # Provide data_source tag
            meta.setdefault("data_source", "esef")

        self._registry_cache = raw
        return raw
