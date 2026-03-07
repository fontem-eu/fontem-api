"""
Historical Price & Market Data Fetcher
========================================
Thin wrapper around `yfinance` that provides the OHLCV history and basic
market statistics needed by the analysis and backtesting modules.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


class PriceFetcher:
    """
    Fetches historical OHLCV data and live market statistics via yfinance.

    All returned DataFrames have a tz-naive DatetimeIndex (UTC stripped) so
    they compare cleanly with plain Python ``datetime`` objects.
    """

    # ------------------------------------------------------------------
    def get_history(
        self,
        ticker: str,
        period: str = "3y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """
        Return a DataFrame with columns Open, High, Low, Close, Volume.

        Uses ``yf.download()`` (v8 chart endpoint) instead of
        ``Ticker.history()`` to avoid the cold-start timeout caused by
        yfinance fetching ``info`` just to determine the ticker's timezone.

        Parameters
        ----------
        ticker:   Stock symbol (e.g. ``"AAPL"``).
        period:   yfinance period string – ``"1mo"``, ``"3mo"``, ``"6mo"``,
                  ``"1y"``, ``"2y"``, ``"3y"``, ``"5y"``, ``"10y"``, ``"max"``.
        interval: ``"1d"`` (daily), ``"1wk"`` (weekly), ``"1mo"`` (monthly).
        """
        for attempt in range(2):
            hist = yf.download(
                ticker,
                period=period,
                interval=interval,
                auto_adjust=True,
                progress=False,
                multi_level_index=False,
            )
            if not hist.empty:
                break
            logger.debug(
                "yf.download empty for %s on attempt %d – retrying…", ticker, attempt + 1
            )

        if hist.empty:
            raise ValueError(f"No price history returned for '{ticker}' "
                             f"(period={period}, interval={interval})")

        # Strip timezone so comparisons with naive datetimes are straightforward
        if hist.index.tz is not None:
            hist.index = hist.index.tz_localize(None)

        return hist[["Open", "High", "Low", "Close", "Volume"]]

    # ------------------------------------------------------------------
    def get_current_price(self, ticker: str) -> float:
        """Return the most recent daily closing price."""
        hist = self.get_history(ticker, period="5d")
        if hist.empty:
            raise ValueError(f"Cannot determine current price for '{ticker}'")
        return float(hist["Close"].iloc[-1])

    # ------------------------------------------------------------------
    def get_info(self, ticker: str) -> dict:
        """Return the yfinance ``info`` dictionary (best-effort)."""
        try:
            return yf.Ticker(ticker).info or {}
        except Exception as exc:
            logger.debug("yfinance.info failed for %s: %s", ticker, exc)
            return {}

    # ------------------------------------------------------------------
    def get_shares_outstanding(self, ticker: str) -> Optional[float]:
        """Return shares outstanding, or ``None`` if unavailable."""
        info = self.get_info(ticker)
        val = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        return float(val) if val else None

    # ------------------------------------------------------------------
    def get_dividend_yield(self, ticker: str) -> float:
        """Return the trailing 12-month dividend yield (0.0 if not a payer)."""
        info = self.get_info(ticker)
        return float(info.get("dividendYield") or 0.0)

    # ------------------------------------------------------------------
    def get_trailing_dividends_per_share(self, ticker: str) -> float:
        """Return trailing 12-month dividends per share."""
        info = self.get_info(ticker)
        return float(info.get("trailingAnnualDividendRate") or 0.0)
