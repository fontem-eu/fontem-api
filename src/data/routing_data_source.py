"""
Routing Data Source
===================
Dispatches requests to either the EU (ESEF) or NA (EDGAR) data source based
on registry membership, not ticker format heuristics.

At construction time the full list of EU tickers is loaded from the ESEF
registry and stored as a frozenset.  Every routing decision is an O(1) set
lookup: if the ticker is in the EU registry it goes to the EU source,
otherwise it goes to the NA source.

This replaces the previous regex approach which
misrouted legitimate NA tickers like ``BRK.A`` or ``BRK.B`` to the EU path.

Ticker search merges results from both sources, EU-first.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..analysis.gmr_data_source import FinancialDataSource, MarketSnapshot

logger = logging.getLogger(__name__)


class RoutingDataSource(FinancialDataSource):
    """
    Routes each request to the appropriate regional data source.

    Parameters
    ----------
    na_source:
        North American (EDGAR-backed) data source.
    eu_source:
        European (ESEF-backed) data source.
    """

    def __init__(
        self,
        na_source: FinancialDataSource,
        eu_source: FinancialDataSource,
    ) -> None:
        self._na = na_source
        self._eu = eu_source

        # Build an O(1) lookup set from the EU registry at startup.
        # EU ticker dicts carry a "ticker" key with the full symbol
        # (e.g. "ASML.AS"); fall back to "symbol" for stubs / tests.
        self._eu_tickers: frozenset[str] = frozenset(
            t.get("ticker") or t.get("symbol", "")
            for t in eu_source.get_available_tickers()
            if t.get("ticker") or t.get("symbol")
        )
        logger.info(
            "RoutingDataSource ready: %d EU tickers indexed",
            len(self._eu_tickers),
        )

    # ------------------------------------------------------------------
    # FinancialDataSource interface
    # ------------------------------------------------------------------

    def get_annual_fundamentals(self, ticker: str, years: int = 10) -> dict:
        return self._route(ticker).get_annual_fundamentals(ticker, years)

    def get_annual_avg_prices(self, ticker: str, years: int = 10) -> pd.Series:
        return self._route(ticker).get_annual_avg_prices(ticker, years)

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        return self._route(ticker).get_annual_dividends(ticker)

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        return self._route(ticker).get_price_history(ticker, period)

    def get_market_snapshot(self, ticker: str) -> MarketSnapshot:
        return self._route(ticker).get_market_snapshot(ticker)

    def get_data_source_name(self, ticker: str) -> str:
        return self._route(ticker).get_data_source_name(ticker)

    # ------------------------------------------------------------------
    # Ticker discovery — EU results first, then NA
    # ------------------------------------------------------------------

    def get_available_tickers(self) -> list[dict]:
        return self._eu.get_available_tickers() + self._na.get_available_tickers()

    def search_tickers(self, query: str, limit: int = 10) -> list[dict]:
        eu_results = self._eu.search_tickers(query, limit)
        remaining = max(0, limit - len(eu_results))
        na_results = self._na.search_tickers(query, remaining) if remaining else []
        return eu_results + na_results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _route(self, ticker: str) -> FinancialDataSource:
        """Return the EU source if ticker is in the EU registry, NA otherwise."""
        return self._eu if ticker in self._eu_tickers else self._na
