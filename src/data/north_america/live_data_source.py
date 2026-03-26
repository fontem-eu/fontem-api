"""
Live Data Source
=================
Concrete implementation of FinancialDataSource backed entirely by locally
downloaded files: SEC EDGAR bulk data (via LocalEdgarFetcher) and EOD price
CSVs (via LocalPriceFetcher).

No network calls, no caching layer.
"""
from __future__ import annotations

import logging
import pandas as pd

from .local_edgar_fetcher import LocalEdgarFetcher
from .local_price_fetcher import LocalPriceFetcher
from ...analysis.gmr_data_source import FinancialDataSource, MarketSnapshot

logger = logging.getLogger(__name__)


class LiveDataSource(FinancialDataSource):
    """
    Production data source that reads all data from locally stored files.

    Parameters
    ----------
    local_data_dir:
        Path to the EDGAR bulk-data directory (must contain ``companyfacts/``
        and ``submissions/`` sub-directories, as written by edgar-data-fetcher).
    local_price_data_dir:
        Path to the price data directory (must contain a ``daily/`` sub-directory
        with one CSV per ticker, as written by usa-stock-price-fetcher).
    """

    def __init__(
        self,
        local_data_dir: str,
        local_price_data_dir: str,
    ) -> None:
        self._edgar = LocalEdgarFetcher(local_data_dir=local_data_dir)
        self._price = LocalPriceFetcher(price_data_dir=local_price_data_dir)
        logger.info(
            "LiveDataSource: EDGAR from %s, prices from %s",
            local_data_dir,
            local_price_data_dir,
        )

    # ------------------------------------------------------------------
    # FinancialDataSource interface
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

    def get_market_snapshot(self, ticker: str) -> MarketSnapshot:
        raw = self._price.get_snapshot(ticker)
        last_div = raw.get("last_dividend") or {}
        return MarketSnapshot(
            current_price=raw.get("current_price"),
            avg_volume=raw.get("avg_volume"),
            shares_outstanding=raw.get("shares_outstanding"),
            last_dividend_date=last_div.get("date"),
            last_dividend_amount=last_div.get("amount"),
            splits=raw.get("splits", pd.Series(dtype=float)),
            latest_quarter=raw.get("latest_quarter") or {},
            beta=raw.get("beta"),
            week_52_high=raw.get("week_52_high"),
            week_52_low=raw.get("week_52_low"),
            market_cap=raw.get("market_cap"),
        )

    def get_data_source_name(self, ticker: str) -> str:  # pylint: disable=unused-argument
        return "edgar"

    # ------------------------------------------------------------------
    # Ticker discovery
    # ------------------------------------------------------------------

    def get_available_tickers(self) -> list[dict]:
        """Return the full list of EDGAR-registered companies with metadata."""
        return self._edgar.get_edgar_ticker_list()

    def search_tickers(self, query: str, limit: int = 10) -> list[dict]:
        """Search tickers by name, symbol, or keywords (case-insensitive)."""
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
