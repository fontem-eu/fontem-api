"""
Live Data Source
=================
Concrete implementation of GMRDataSource that pulls data from the SEC EDGAR
API (via EdgarFetcher) and Yahoo Finance (via PriceFetcher).

Inject this into GMRLong / GMRShort for production use.
Inject a MockDataSource in unit tests to avoid any network traffic.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .edgar_fetcher import EdgarFetcher
from .price_fetcher import PriceFetcher
from ..analysis.gmr_data_source import GMRDataSource


class LiveDataSource(GMRDataSource):
    """
    Production adapter that satisfies the GMRDataSource port using the two
    fetcher classes already implemented in this project.

    Example::

        ds = LiveDataSource()                   # uses default identities
        result = GMRLong(ds).compute("KO")
    """

    def __init__(
        self,
        edgar_fetcher: Optional[EdgarFetcher] = None,
        price_fetcher: Optional[PriceFetcher] = None,
        edgar_identity: str = "bemar-edgar@research.com",
    ) -> None:
        self._edgar = edgar_fetcher or EdgarFetcher(identity=edgar_identity)
        self._price = price_fetcher or PriceFetcher()

    # ------------------------------------------------------------------
    def get_annual_fundamentals(self, ticker: str, years: int = 10) -> dict:
        return self._edgar.fetch_fundamentals(ticker, years=years)

    def get_annual_avg_prices(self, ticker: str, years: int = 10) -> pd.Series:
        period = f"{min(years, 10)}y"
        return self._price.get_annual_avg_prices(ticker, period=period)

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        return self._price.get_annual_dividends(ticker)

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        return self._price.get_history(ticker, period=period)

    def get_market_snapshot(self, ticker: str) -> dict:
        return {
            "current_price":      self._price.get_current_price(ticker),
            "avg_volume":         self._price.get_avg_volume(ticker),
            "shares_outstanding": self._price.get_shares_outstanding(ticker),
            "last_dividend":      self._price.get_last_dividend(ticker),
            "splits":             self._price.get_splits(ticker),
            "latest_quarter":     self._price.get_latest_quarter(ticker),
        }
