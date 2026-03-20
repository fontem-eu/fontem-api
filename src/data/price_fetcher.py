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

        # yfinance ≥1.x returns lowercase column names; normalise to Title Case
        # so downstream code can always use "Open", "High", "Low", "Close", "Volume".
        hist.columns = [c.title() if isinstance(c, str) else c for c in hist.columns]

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
        except Exception as exc:  # pylint: disable=broad-exception-caught
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

    # ------------------------------------------------------------------
    def get_avg_volume(self, ticker: str) -> Optional[float]:
        """Return the 3-month average daily trading volume, or None."""
        info = self.get_info(ticker)
        val = info.get("averageVolume") or info.get("averageDailyVolume3Month")
        return float(val) if val else None

    # ------------------------------------------------------------------
    def get_annual_avg_prices(self, ticker: str, period: str = "10y") -> pd.Series:
        """
        Return the average closing price for each calendar year as a
        pd.Series indexed by integer year (descending).

        Computed by taking the mean of all daily closes within each year
        from the OHLCV history.
        """
        hist = self.get_history(ticker, period=period)
        if hist.empty:
            return pd.Series(dtype=float)
        annual = (
            hist["Close"]
            .groupby(hist.index.year)
            .mean()
            .sort_index(ascending=False)
        )
        annual.index = annual.index.astype(int)
        return annual

    # ------------------------------------------------------------------
    def get_annual_dividends(self, ticker: str) -> pd.Series:
        """
        Return total dividends paid per calendar year as a pd.Series
        indexed by integer year (descending).

        Aggregates individual dividend events from yfinance, which is
        typically quarterly for US stocks.
        """
        try:
            divs = yf.Ticker(ticker).dividends
            if divs is None or divs.empty:
                return pd.Series(dtype=float)
            if divs.index.tz is not None:
                divs.index = divs.index.tz_localize(None)
            annual = divs.groupby(divs.index.year).sum().sort_index(ascending=False)
            annual.index = annual.index.astype(int)
            return annual
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("get_annual_dividends failed for %s: %s", ticker, exc)
            return pd.Series(dtype=float)

    # ------------------------------------------------------------------
    def get_last_dividend(self, ticker: str) -> dict:
        """
        Return the most recent dividend event as ``{"date": str, "amount": float}``.
        Returns ``{"date": None, "amount": 0.0}`` if none found.
        """
        try:
            divs = yf.Ticker(ticker).dividends
            if divs is None or divs.empty:
                return {"date": None, "amount": 0.0}
            if divs.index.tz is not None:
                divs.index = divs.index.tz_localize(None)
            last = divs.iloc[-1]
            last_date = divs.index[-1]
            return {
                "date": str(last_date.date()) if hasattr(last_date, "date") else str(last_date),
                "amount": float(last),
            }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("get_last_dividend failed for %s: %s", ticker, exc)
            return {"date": None, "amount": 0.0}

    # ------------------------------------------------------------------
    def get_splits(self, ticker: str) -> pd.Series:
        """
        Return stock split history as a pd.Series of split ratios indexed
        by integer year (descending).  Consolidates multiple splits in the
        same year by multiplying them together.

        A ratio > 1 means forward split (e.g. 4.0 = 4-for-1).
        Returns an empty Series if no splits exist.
        """
        try:
            splits = yf.Ticker(ticker).splits
            if splits is None or splits.empty:
                return pd.Series(dtype=float)
            if splits.index.tz is not None:
                splits.index = splits.index.tz_localize(None)
            # Combine multiple splits in the same year by multiplying
            annual = splits.groupby(splits.index.year).prod().sort_index(ascending=False)
            annual.index = annual.index.astype(int)
            return annual
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("get_splits failed for %s: %s", ticker, exc)
            return pd.Series(dtype=float)

    # ------------------------------------------------------------------
    def get_snapshot(self, ticker: str) -> dict:  # pylint: disable=too-many-locals,too-many-statements
        """
        Return a single market-snapshot dict by reusing one ``yf.Ticker`` instance
        for all fields, rather than creating a separate instance per call.

        Keys returned:
            current_price, avg_volume, shares_outstanding,
            last_dividend (dict), splits (pd.Series), latest_quarter (dict)
        """
        t = yf.Ticker(ticker)

        # ── info (shares, volume) ─────────────────────────────────────
        try:
            info = t.info or {}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("yfinance.info failed for %s: %s", ticker, exc)
            info = {}

        shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        volume = info.get("averageVolume") or info.get("averageDailyVolume3Month")

        # ── current price via recent OHLCV ────────────────────────────
        try:
            hist5d = self.get_history(ticker, period="5d")
            current_price = float(hist5d["Close"].iloc[-1]) if not hist5d.empty else float("nan")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("get_history failed for %s: %s", ticker, exc)
            current_price = float("nan")

        # ── dividends ─────────────────────────────────────────────────
        last_dividend: dict = {"date": None, "amount": 0.0}
        try:
            divs = t.dividends
            if divs is not None and not divs.empty:
                if divs.index.tz is not None:
                    divs = divs.copy()
                    divs.index = divs.index.tz_localize(None)
                last = divs.iloc[-1]
                last_date = divs.index[-1]
                last_dividend = {
                    "date": str(last_date.date()) if hasattr(last_date, "date") else str(last_date),
                    "amount": float(last),
                }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("dividends failed for %s: %s", ticker, exc)

        # ── splits ────────────────────────────────────────────────────
        splits_series: pd.Series = pd.Series(dtype=float)
        try:
            raw_splits = t.splits
            if raw_splits is not None and not raw_splits.empty:
                if raw_splits.index.tz is not None:
                    raw_splits = raw_splits.copy()
                    raw_splits.index = raw_splits.index.tz_localize(None)
                splits_series = (
                    raw_splits.groupby(raw_splits.index.year)
                    .prod()
                    .sort_index(ascending=False)
                )
                splits_series.index = splits_series.index.astype(int)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("splits failed for %s: %s", ticker, exc)

        # ── latest quarter (balance sheet) ────────────────────────────
        latest_quarter: dict = {}
        try:
            qbs = t.quarterly_balance_sheet
            if qbs is not None and not qbs.empty:
                col = qbs.columns[0]
                row = qbs[col]

                def _get(*keys) -> Optional[float]:
                    for k in keys:
                        if k in row.index:
                            val = row[k]
                            if pd.notna(val):
                                return float(val)
                    return None

                latest_quarter = {
                    "as_of":               str(col.date()) if hasattr(col, "date") else str(col),
                    "current_assets":      _get("Current Assets", "Total Current Assets"),
                    "inventory":           _get("Inventory"),
                    "prepaid_expenses":    _get("Prepaid Expenses", "Other Current Assets",
                                                "Prepaid And Other Current Assets"),
                    "current_liabilities": _get("Current Liabilities", "Total Current Liabilities"),
                    "total_liabilities":   _get("Total Liabilities Net Minority Interest",
                                                "Total Liabilities"),
                    "total_debt":          _get("Total Debt",
                                                "Long Term Debt And Capital Lease Obligation",
                                                "Long Term Debt", "Net Debt"),
                    "equity":              _get("Stockholders Equity",
                                                "Total Equity Gross Minority Interest",
                                                "Common Stock Equity"),
                    "shares_outstanding":  _get("Ordinary Shares Number", "Share Issued",
                                                "Common Stock"),
                }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("quarterly_balance_sheet failed for %s: %s", ticker, exc)

        beta         = info.get("beta")
        week_52_high = info.get("fiftyTwoWeekHigh")
        week_52_low  = info.get("fiftyTwoWeekLow")

        return {
            "current_price":      current_price,
            "avg_volume":         float(volume) if volume else None,
            "shares_outstanding": float(shares) if shares else None,
            "last_dividend":      last_dividend,
            "splits":             splits_series,
            "latest_quarter":     latest_quarter,
            "beta":               float(beta) if beta is not None else None,
            "week_52_high":       float(week_52_high) if week_52_high is not None else None,
            "week_52_low":        float(week_52_low) if week_52_low is not None else None,
        }

    # ------------------------------------------------------------------
    def get_latest_quarter(self, ticker: str) -> dict:
        """
        Return a snapshot of the most recent quarterly balance sheet values.

        Keys returned:
            current_assets, inventory, prepaid_expenses, current_liabilities,
            total_liabilities, equity, shares_outstanding

        Values are floats (or None if not reported).  Data is sourced from
        yfinance's quarterly balance sheet which lags 10-Q filings by ~45 days.
        """
        try:
            t = yf.Ticker(ticker)
            qbs = t.quarterly_balance_sheet
            if qbs is None or qbs.empty:
                return {}
            # Most recent quarter is the first column
            col = qbs.columns[0]
            row = qbs[col]

            def _get(*keys) -> Optional[float]:
                for k in keys:
                    if k in row.index:
                        val = row[k]
                        if pd.notna(val):
                            return float(val)
                return None

            return {
                "as_of":              str(col.date()) if hasattr(col, "date") else str(col),
                "current_assets":     _get("Current Assets", "Total Current Assets"),
                "inventory":          _get("Inventory"),
                "prepaid_expenses":   _get("Prepaid Expenses", "Other Current Assets",
                                           "Prepaid And Other Current Assets"),
                "current_liabilities":_get("Current Liabilities", "Total Current Liabilities"),
                "total_liabilities":  _get("Total Liabilities Net Minority Interest",
                                           "Total Liabilities"),
                "total_debt":         _get("Total Debt",
                                           "Long Term Debt And Capital Lease Obligation",
                                           "Long Term Debt",
                                           "Net Debt"),
                "equity":             _get("Stockholders Equity",
                                           "Total Equity Gross Minority Interest",
                                           "Common Stock Equity"),
                "shares_outstanding": _get("Ordinary Shares Number", "Share Issued",
                                           "Common Stock"),
            }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("get_latest_quarter failed for %s: %s", ticker, exc)
            return {}
