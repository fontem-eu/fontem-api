"""
Routing Data Source
===================
Dispatches requests to either the EU (ESEF) or NA (EDGAR) data source based
on ticker format.

European tickers follow the exchange-suffix convention: ``SYMBOL.EXCHANGE``
(e.g. ``ASML.AS``, ``SAP.DE``).  Everything else is routed to the North
American source.

Ticker search merges results from both sources, EU-first.
"""
from __future__ import annotations

import re

import pandas as pd

from ..analysis.gmr_data_source import FinancialDataSource

# Pattern: one or more uppercase letters/digits, a dot, then 1–3 uppercase letters.
_EU_PATTERN = re.compile(r"^[A-Z0-9]+\.[A-Z]{1,3}$")


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

    def get_market_snapshot(self, ticker: str) -> dict:
        return self._route(ticker).get_market_snapshot(ticker)

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
        """Return the EU source for exchange-suffix tickers, NA source otherwise."""
        return self._eu if _EU_PATTERN.match(ticker) else self._na


def get_data_source_name(ticker: str) -> str:
    """Return ``'esef'`` for EU-pattern tickers, ``'edgar'`` otherwise."""
    return "esef" if _EU_PATTERN.match(ticker) else "edgar"
