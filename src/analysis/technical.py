"""
GMR Short-Term Technical Indicator  (Python port)
===================================================
A faithful translation of the C# ``GMRShort`` class written by the author's
brother, plus small enhancements for use with pandas/yfinance data.

Algorithm summary
-----------------
Using the last ``LOOKBACK_MONTHS`` (6) months of daily OHLCV data the
indicator computes three core metrics:

1. **Win Probability**
   Rank every trading day by its daily close-to-close return (ascending).
   The win probability is the percentile rank of the *first* day with a
   positive return.  A value > 0.5 means statistically more up-days than
   down-days.

2. **VUp / VDown (monthly volatility potential)**
   For each trading day *d* in month *m*:
     VUp[d]   = max(High  of all days in month m from d onward) / Low[d]  − 1
     VDown[d] = −1 / (min(Low of all days in month m from d onward) / High[d]) + 1
   Per month:  monthly_vup = max VUp in that month
               monthly_vdown = min VDown in that month  (most negative value)
   Averages across all months yield avg_vup / avg_vdown.

3. **MAT  (Moving Average Trend)**
   43-trading-day simple moving average of Close.
   mat_diff_pct = (MAT − current_price) / current_price
   A small negative value (> −2.5 %) means price is near or above the MA
   → upward momentum.

All three criteria plus a volume filter must pass for a BUY signal (matching
the original ``volatilityOK && priceOK && probabilityOK && matOK`` logic).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants  (mirroring GMRTool appsettings.json)
# ---------------------------------------------------------------------------

MAT_WINDOW      = 43        # trading days  (~2 calendar months)
LOOKBACK_MONTHS = 6         # months of history used per evaluation

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "win_prob_min":  0.50,          # win probability > 50 %
    "vup_min":       0.30,          # average monthly VUp > 30 %
    "vdown_max":    -0.30,          # average monthly VDown < −30 %
    "mat_diff_min": -0.025,         # price not more than 2.5 % below 43d MA
    "min_volume":    1_000_000.0,   # average daily volume
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TechnicalScore:
    """All GMR technical-analysis outputs for one stock at one point in time."""

    ticker:         str
    analysis_date:  datetime
    current_price:  float
    current_volume: float = 0.0

    # ---- GMR metrics -------------------------------------------------------
    win_probability: Optional[float] = None
    avg_vup:         Optional[float] = None
    avg_vdown:       Optional[float] = None
    mat:             Optional[float] = None
    mat_diff_pct:    Optional[float] = None

    # ---- monthly breakdown (for display) -----------------------------------
    monthly_vup:   Dict[str, float] = field(default_factory=dict)
    monthly_vdown: Dict[str, float] = field(default_factory=dict)

    # ---- per-check pass/fail -----------------------------------------------
    checks: Dict[str, bool] = field(default_factory=dict)

    # ---- aggregate ---------------------------------------------------------
    score:           float = 0.0
    signal_strength: str   = "NEUTRAL"


# ---------------------------------------------------------------------------
# Analyser
# ---------------------------------------------------------------------------

class TechnicalAnalyzer:
    """
    Runs the GMR Short-Term indicator on a DataFrame of daily OHLCV data.

    Parameters
    ----------
    thresholds:
        Override any key in ``DEFAULT_THRESHOLDS``.
    """

    def __init__(self, thresholds: Optional[Dict[str, float]] = None):
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    # ------------------------------------------------------------------
    def analyze(
        self,
        price_history: pd.DataFrame,
        ticker: str = "UNKNOWN",
        analysis_date: Optional[datetime] = None,
    ) -> TechnicalScore:
        """
        Run the GMR analysis.

        Parameters
        ----------
        price_history:
            Daily OHLCV DataFrame from ``PriceFetcher.get_history()``.
            Index must be a DatetimeIndex (tz-naive is preferred).
        ticker:
            Ticker symbol (used only for labelling results).
        analysis_date:
            The date *as of* which to analyse (defaults to the last row in
            *price_history*).  Rows after this date are ignored so the method
            can be called for any historical point in time (backtesting).
        """
        if price_history.empty:
            raise ValueError(f"Empty price_history for {ticker}")

        # Normalise index to tz-naive
        hist = price_history.copy()
        if hist.index.tz is not None:
            hist.index = hist.index.tz_localize(None)

        if analysis_date is None:
            analysis_date = hist.index[-1].to_pydatetime()

        # ── Slice to analysis window ──────────────────────────────────
        as_of    = pd.Timestamp(analysis_date).normalize()
        cutoff   = as_of - pd.DateOffset(months=LOOKBACK_MONTHS)
        window   = hist[(hist.index >= cutoff) & (hist.index <= as_of)].copy()

        current_price  = float(window["Close"].iloc[-1])
        current_volume = float(window["Volume"].iloc[-1])

        score = TechnicalScore(
            ticker=ticker,
            analysis_date=analysis_date,
            current_price=current_price,
            current_volume=current_volume,
        )

        if len(window) < 20:
            logger.warning("%s: only %d days in window – skipping GMR", ticker, len(window))
            return score

        # ── Daily returns ─────────────────────────────────────────────
        window["change"] = window["Close"].pct_change()
        window = window.dropna(subset=["change"])

        # ── 1. Win Probability ────────────────────────────────────────
        n = len(window)
        sorted_w = window.sort_values("change").reset_index(drop=False)
        sorted_w["prob"] = [(i + 1) / n for i in range(n)]

        positive = sorted_w[sorted_w["change"] > 0]
        win_prob = float(positive["prob"].iloc[0]) if not positive.empty else 0.0

        # ── 2. VUp / VDown per calendar month ─────────────────────────
        window["ym"] = window.index.to_period("M")

        monthly_vup:   Dict[str, float] = {}
        monthly_vdown: Dict[str, float] = {}

        for period, month_df in window.groupby("ym"):
            if len(month_df) < 2:
                continue
            vups, vdowns = [], []

            for idx, row in month_df.iterrows():
                future = month_df[month_df.index >= idx]
                if future.empty or row["Low"] <= 0 or row["High"] <= 0:
                    continue
                vup   = (future["High"].max() / row["Low"]) - 1.0
                vdown = -1.0 / (future["Low"].min() / row["High"]) + 1.0
                vups.append(vup)
                vdowns.append(vdown)

            if vups:
                key = str(period)
                monthly_vup[key]   = float(np.max(vups))
                monthly_vdown[key] = float(np.min(vdowns))

        avg_vup   = float(np.mean(list(monthly_vup.values())))   if monthly_vup   else 0.0
        avg_vdown = float(np.mean(list(monthly_vdown.values()))) if monthly_vdown else 0.0

        # ── 3. MAT (43-trading-day moving average) ────────────────────
        data_to_date = hist[hist.index <= as_of]
        mat = float(data_to_date["Close"].iloc[-MAT_WINDOW:].mean()) \
              if len(data_to_date) >= MAT_WINDOW \
              else float(data_to_date["Close"].mean())

        mat_diff_pct = (mat - current_price) / current_price if current_price > 0 else 0.0

        # ── Per-check evaluation ──────────────────────────────────────
        t = self.thresholds
        checks: Dict[str, bool] = {
            "win_prob":  win_prob   >  t["win_prob_min"],
            "avg_vup":   avg_vup    >  t["vup_min"],
            "avg_vdown": avg_vdown  <  t["vdown_max"],   # must be sufficiently negative
            "mat_diff":  mat_diff_pct > t["mat_diff_min"],
            "volume":    current_volume >= t["min_volume"],
        }

        passed = sum(v for v in checks.values())
        raw_score = (passed / len(checks)) * 100.0

        # ── Signal label ──────────────────────────────────────────────
        if raw_score >= 80:
            strength = "STRONG_BUY"
        elif raw_score >= 60:
            strength = "BUY"
        elif raw_score >= 40:
            strength = "HOLD"
        elif raw_score >= 20:
            strength = "SELL"
        else:
            strength = "STRONG_SELL"

        score.win_probability = win_prob
        score.avg_vup         = avg_vup
        score.avg_vdown       = avg_vdown
        score.mat             = mat
        score.mat_diff_pct    = mat_diff_pct
        score.monthly_vup     = monthly_vup
        score.monthly_vdown   = monthly_vdown
        score.checks          = checks
        score.score           = raw_score
        score.signal_strength = strength

        return score
