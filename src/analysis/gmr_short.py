"""
GMR Short-Term Swing-Trading Screen
=====================================
Evaluates a stock for short-term volatility-based swing trading.
Uses 6 months of daily OHLCV, computing the metrics from GMRShort.cs.

Key metrics
-----------
  win_probability  – fraction of days with a non-negative daily return.
                     Computed via rank: sort days by change descending,
                     assign probability = rank / n_days, then return the
                     probability of the *boundary* positive day.  This equals
                     the empirical fraction of positive-return days.

  VUp(day)         – max(high[j] for j in same month, j ≥ today) / low[today] − 1
                     "If I enter at today's low, what's my max upside this month?"

  VDown(day)       – 1 − today_high / min(low[j] for j in same month, j ≥ today)
                     "If I enter at today's high, what's my max downside this month?"
                     (Matches the C# formula: -1/(min_low/high) + 1)

  Monthly VUp/VDown – max VUp / min VDown for the month.
  Average VUp/VDown – mean across all 6-month months.

  MAT              – 43-trading-day moving average of close (most recent 43 days).
  diffMAT          – (MAT − current_price) / current_price.
                     Positive when price < MAT (mean-reversion buy signal).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .gmr_data_source import GMRDataSource, GMRSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class GMRShortResult:
    """Structured output of the GMR short-term screen."""
    ticker: str
    current_price: float
    avg_volume: float
    win_probability: float
    avg_v_up: float
    avg_v_down: float
    mat_43d: float           # 43-day moving average
    diff_mat_pct: float      # (MAT − price) / price

    # Per-month breakdown: index = monthly Period, columns = [v_up, v_down]
    monthly_breakdown: pd.DataFrame

    # Pass/fail per criterion
    flags: dict
    passes_all: bool


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class GMRShort:
    """
    GMR Short-term screen.

    Example::

        ds = LiveDataSource(...)
        result = GMRShort(ds).compute("XYZ")
        print(result.passes_all, result.win_probability)
    """

    def __init__(
        self,
        data_source: GMRDataSource,
        settings: Optional[GMRSettings] = None,
    ) -> None:
        self._ds = data_source
        self._s = settings or GMRSettings()

    # ------------------------------------------------------------------
    def compute(self, ticker: str) -> GMRShortResult:
        """Run the short-term GMR screen and return a :class:`GMRShortResult`."""
        snapshot      = self._ds.get_market_snapshot(ticker)
        current_price = float(snapshot.get("current_price", float("nan")))
        avg_volume    = float(snapshot.get("avg_volume", 0) or 0)

        hist = self._ds.get_price_history(ticker, period="1y")
        if hist.empty:
            return self._empty_result(ticker, current_price, avg_volume)

        # ── 6-month window: discard the older half of the 12-month pull ──
        max_date   = hist.index.max()
        limit_date = max_date - pd.DateOffset(months=6)
        window = hist[(hist.index > limit_date) & (hist.index <= max_date)].copy()

        if len(window) < 5:
            return self._empty_result(ticker, current_price, avg_volume)

        window = window.sort_index(ascending=True)

        # ── Daily return: (close_t − close_{t-1}) / close_{t-1} ─────────
        closes  = window["Close"].values.astype(float)
        changes = np.zeros(len(closes))
        for i in range(1, len(closes)):
            prev       = closes[i - 1]
            changes[i] = (closes[i] - prev) / prev if prev != 0 else 0.0
        window = window.copy()
        window["change"] = changes

        # ── Win probability via rank assignment ──────────────────────────
        # Sort by change descending; probability = rank / n_days.
        # The boundary positive-day's probability equals the fraction of
        # non-negative days (empirical win rate).
        n = len(window)
        window["probability"] = (
            window["change"]
            .rank(ascending=False, method="first")
            .astype(float)
            .div(n)
        )

        # ── Per-day VUp / VDown (forward-looking within calendar month) ──
        v_ups   = np.full(n, float("nan"))
        v_downs = np.full(n, float("nan"))

        dates   = window.index
        highs   = window["High"].values.astype(float)
        lows    = window["Low"].values.astype(float)

        for i in range(n):
            date  = dates[i]
            low_i = lows[i]
            hi_i  = highs[i]

            # Rows in the same calendar month, on or after today
            mask = (
                (dates.year  == date.year)  &
                (dates.month == date.month) &
                (dates >= date)
            )
            fwd_highs = highs[mask]
            fwd_lows  = lows[mask]

            if fwd_highs.size == 0:
                continue

            max_hi  = float(fwd_highs.max())
            min_lo  = float(fwd_lows.min())

            v_ups[i]   = (max_hi / low_i) - 1.0        if low_i > 0  else float("nan")
            # C# formula: -1 / (min_low / high) + 1  =  1 - high / min_low
            v_downs[i] = 1.0 - (hi_i / min_lo)         if min_lo > 0 else float("nan")

        window["v_up"]   = v_ups
        window["v_down"] = v_downs

        # ── Monthly aggregation ──────────────────────────────────────────
        periods = window.index.to_period("M")
        monthly = (
            window
            .groupby(periods)
            .agg(v_up=("v_up", "max"), v_down=("v_down", "min"))
        )
        avg_v_up   = float(np.nanmean(monthly["v_up"].values))
        avg_v_down = float(np.nanmean(monthly["v_down"].values))

        # ── Win probability ──────────────────────────────────────────────
        # Sort ascending by change; find the probability of the first >= 0 day.
        sorted_asc   = window.sort_values(["change", "probability"], ascending=True)
        positive_rows = sorted_asc[sorted_asc["change"] >= 0]
        win_probability = (
            float(positive_rows.iloc[0]["probability"])
            if not positive_rows.empty else 0.0
        )

        # ── 43-day moving average (MAT) ──────────────────────────────────
        recent_43 = window.sort_index(ascending=False).head(43)
        mat_43d   = float(recent_43["Close"].mean())
        diff_mat  = (
            (mat_43d - current_price) / current_price
            if current_price and current_price > 0 else float("nan")
        )

        # ── Evaluate against thresholds ──────────────────────────────────
        s = self._s
        flags = {
            "volume":      avg_volume    >  s.min_volume,
            "price_range": s.min_price   <= current_price <= s.max_price,
            "win_prob":    win_probability > s.win_probability,
            "volatility":  avg_v_up > s.trigger_v_up and avg_v_down < s.trigger_v_down,
            "mat":         diff_mat  > s.diff_mat if diff_mat == diff_mat else False,
        }
        passes_all = all(flags.values())

        return GMRShortResult(
            ticker=ticker.upper(),
            current_price=current_price,
            avg_volume=avg_volume,
            win_probability=win_probability,
            avg_v_up=avg_v_up,
            avg_v_down=avg_v_down,
            mat_43d=mat_43d,
            diff_mat_pct=diff_mat,
            monthly_breakdown=monthly,
            flags=flags,
            passes_all=passes_all,
        )

    # ------------------------------------------------------------------
    def _empty_result(
        self, ticker: str, price: float, volume: float
    ) -> GMRShortResult:
        return GMRShortResult(
            ticker=ticker.upper(),
            current_price=price, avg_volume=volume,
            win_probability=0.0, avg_v_up=0.0, avg_v_down=0.0,
            mat_43d=float("nan"), diff_mat_pct=float("nan"),
            monthly_breakdown=pd.DataFrame(),
            flags={k: False for k in
                   ("volume", "price_range", "win_prob", "volatility", "mat")},
            passes_all=False,
        )
