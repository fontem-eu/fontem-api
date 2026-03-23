"""
Local Price Fetcher
====================
Reads end-of-day OHLCV price data from locally downloaded CSV files
(populated by usa-stock-price-fetcher).

Data layout expected on disk:
    {price_data_dir}/daily/{TICKER}.csv   – Date,Open,High,Low,Close,Volume

If a ticker's CSV file is absent all methods return empty / None results
without raising an exception.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_PERIOD_DAYS = {
    "1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180,
    "1y": 365, "2y": 730, "3y": 1095, "5y": 1825,
    "10y": 3650, "max": 36500,
}


def _period_to_start(period: str) -> date:
    days = _PERIOD_DAYS.get(period)
    if days is None:
        try:
            if period.endswith("y"):
                days = int(period[:-1]) * 365
            elif period.endswith("mo"):
                days = int(period[:-2]) * 30
            elif period.endswith("d"):
                days = int(period[:-1])
            else:
                days = 365
        except ValueError:
            days = 365
    return date.today() - timedelta(days=days)


class LocalPriceFetcher:
    """
    Reads stock price data from locally downloaded CSV files.

    All methods return empty / None when no local CSV exists for the requested
    ticker — no network fallback.
    """

    def __init__(self, price_data_dir: str) -> None:
        self._daily_dir = Path(price_data_dir) / "daily"
        logger.info("LocalPriceFetcher initialised with data dir: %s", price_data_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _csv_path(self, ticker: str) -> Path:
        return self._daily_dir / f"{ticker.upper()}.csv"

    def _has_local(self, ticker: str) -> bool:
        return self._csv_path(ticker).exists()

    def _load_history(self, ticker: str) -> Optional[pd.DataFrame]:
        """Load the full local OHLCV CSV.  Returns None if absent or unreadable."""
        path = self._csv_path(ticker)
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
            df.index = df.index.normalize()
            df.sort_index(inplace=True)
            df.columns = [c.strip().title() for c in df.columns]
            for col in ("Open", "High", "Low", "Close", "Volume"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df.dropna(subset=["Close"], inplace=True)
            return df if not df.empty else None
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to load local price CSV for %s: %s", ticker, exc)
            return None

    def _filter_period(self, df: pd.DataFrame, period: str) -> pd.DataFrame:
        start = pd.Timestamp(_period_to_start(period))
        return df[df.index >= start]

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_history(
        self,
        ticker: str,
        period: str = "3y",
        interval: str = "1d",  # pylint: disable=unused-argument
    ) -> pd.DataFrame:
        """Return OHLCV DataFrame for *ticker*.  Returns empty DataFrame if absent."""
        df = self._load_history(ticker)
        if df is None:
            logger.debug("LocalPriceFetcher: no local data for %s", ticker)
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        filtered = self._filter_period(df, period)
        if filtered.empty:
            logger.debug(
                "LocalPriceFetcher: local data for %s doesn't cover period %s",
                ticker, period,
            )
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        logger.debug(
            "LocalPriceFetcher: served %s history (%d rows, period=%s)",
            ticker, len(filtered), period,
        )
        return filtered[["Open", "High", "Low", "Close", "Volume"]]

    def get_current_price(self, ticker: str) -> float:
        """Return the most recent daily closing price.  Returns 0.0 if absent."""
        df = self._load_history(ticker)
        if df is None:
            logger.debug("LocalPriceFetcher: no local data for %s — returning 0.0", ticker)
            return 0.0
        return float(df["Close"].iloc[-1])

    def get_annual_avg_prices(self, ticker: str, period: str = "10y") -> pd.Series:
        """Return mean closing price per calendar year (int index, descending)."""
        df = self._load_history(ticker)
        if df is None:
            logger.debug("LocalPriceFetcher: no local data for %s", ticker)
            return pd.Series(dtype=float)
        hist = self._filter_period(df, period)
        if hist.empty:
            return pd.Series(dtype=float)
        annual = (
            hist["Close"]
            .groupby(hist.index.year)
            .mean()
            .sort_index(ascending=False)
        )
        annual.index = annual.index.astype(int)
        logger.debug("LocalPriceFetcher: annual prices for %s: %d years", ticker, len(annual))
        return annual

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        """Dividend data is not included in OHLCV files — returns empty Series."""
        logger.debug("LocalPriceFetcher: no local dividend data for %s", ticker)
        return pd.Series(dtype=float)

    def get_snapshot(self, ticker: str) -> dict:
        """
        Build a market snapshot from local price data.

        current_price  = most recent daily closing price
        avg_volume     = mean daily volume over the last 252 trading days
        week_52_high/low = high/low of the last 252 trading days
        shares_outstanding, beta, market_cap = not available locally (None)
        """
        df = self._load_history(ticker)
        if df is None:
            logger.debug(
                "LocalPriceFetcher: no local data for %s — returning empty snapshot", ticker
            )
            return {
                "current_price":      0.0,
                "avg_volume":         0.0,
                "shares_outstanding": None,
                "last_dividend":      {"date": None, "amount": 0.0},
                "splits":             pd.Series(dtype=float),
                "latest_quarter":     {},
                "week_52_high":       None,
                "week_52_low":        None,
                "beta":               None,
                "market_cap":         None,
            }

        last_row      = df.iloc[-1]
        current_price = float(last_row["Close"])
        latest_date   = df.index[-1].date()

        recent       = df.tail(252)
        avg_volume   = float(recent["Volume"].mean())  if "Volume" in df.columns else 0.0
        week_52_high = float(recent["High"].max())     if "High"   in df.columns else None
        week_52_low  = float(recent["Low"].min())      if "Low"    in df.columns else None

        logger.info(
            "LocalPriceFetcher: snapshot for %s — price=%.2f (as of %s)",
            ticker, current_price, latest_date,
        )
        return {
            "current_price":      current_price,
            "avg_volume":         avg_volume,
            "shares_outstanding": None,
            "last_dividend":      {"date": None, "amount": 0.0},
            "splits":             pd.Series(dtype=float),
            "latest_quarter":     {},
            "week_52_high":       week_52_high,
            "week_52_low":        week_52_low,
            "beta":               None,
            "market_cap":         None,
        }

    def get_info(self, ticker: str) -> dict:  # pylint: disable=unused-argument
        """Not available from local OHLCV files — returns empty dict."""
        return {}

    def get_shares_outstanding(self, ticker: str) -> Optional[float]:  # pylint: disable=unused-argument
        """Not available from local OHLCV files — returns None."""
        return None

    def get_splits(self, ticker: str) -> pd.Series:  # pylint: disable=unused-argument
        """Not available from local OHLCV files — returns empty Series."""
        return pd.Series(dtype=float)
