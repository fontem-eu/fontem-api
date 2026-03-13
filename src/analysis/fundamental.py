"""
Fundamental Analysis Module
============================
Analyses long-term financial health using EDGAR 10-K data.

Inspired by the GMRLong indicator (original C# implementation), this module
computes the classic value-investing ratios popularised by Benjamin Graham and
Warren Buffett, then scores them against configurable thresholds to produce a
numeric buy/hold/sell signal.

Metrics computed
----------------
P/E   – Price-to-Earnings            (lower is cheaper)
P/B   – Price-to-Book                (lower = trading near asset value)
D/E   – Debt-to-Equity               (lower = less leverage risk)
ROE   – Return on Equity             (higher = management efficiency)
NPM   – Net Profit Margin            (higher = pricing power / efficiency)
CR    – Current Ratio                (higher = liquidity cushion)
RG    – Revenue CAGR (5-year)        (positive = growing business)
DY    – Dividend Yield               (positive and growing = shareholder returns)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default thresholds
# (sourced directly from the original GMRTool appsettings.json + common sense)
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "pe_max":           20.0,   # P/E ≤ 20   (GMRTool used 15; relaxed slightly)
    "pb_max":            1.5,   # P/B ≤ 1.5
    "de_max":            1.5,   # D/E ≤ 1.5
    "roe_min":           0.15,  # ROE ≥ 15 %
    "npm_min":           0.10,  # Net profit margin ≥ 10 %
    "current_ratio_min": 1.0,   # Current ratio ≥ 1
    "revenue_cagr_min":  0.0,   # Revenue CAGR > 0 %  (growing)
    "div_yield_min":     0.02,  # Dividend yield ≥ 2 %
}

# How much each passing check contributes to the 0-100 score
WEIGHTS: Dict[str, float] = {
    "pe":            15.0,
    "pb":            10.0,
    "de":            15.0,
    "roe":           15.0,
    "npm":           10.0,
    "current_ratio": 10.0,
    "revenue_cagr":  15.0,
    "div_yield":     10.0,
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class FundamentalScore:
    """All fundamental analysis outputs for one stock at one point in time."""

    ticker: str
    current_price: float

    # ---- computed metric values ----------------------------------------
    pe_ratio:              Optional[float] = None
    pb_ratio:              Optional[float] = None
    debt_equity:           Optional[float] = None
    roe:                   Optional[float] = None
    net_profit_margin:     Optional[float] = None
    current_ratio:         Optional[float] = None
    revenue_cagr_5y:       Optional[float] = None
    net_income_cagr_5y:    Optional[float] = None
    dividend_yield:        float = 0.0
    consecutive_profit_yrs: int = 0

    # ---- per-metric pass/fail (True = positive signal) ------------------
    checks: Dict[str, bool] = field(default_factory=dict)

    # ---- aggregate -------------------------------------------------------
    score:           float = 0.0   # 0–100
    signal_strength: str   = "NEUTRAL"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cagr(series: pd.Series, years: int = 5) -> Optional[float]:
    """
    Compound Annual Growth Rate over the most-recent *years* of data.
    *series* must be sorted descending (most-recent first).
    Returns ``None`` if insufficient data or negative base value.
    """
    if series is None or len(series) < 2:
        return None
    n = min(years, len(series) - 1)
    end_val   = series.iloc[0]
    start_val = series.iloc[n]
    if start_val <= 0 or end_val <= 0:
        return None
    return (end_val / start_val) ** (1.0 / n) - 1.0


def _consecutive_positive(series: pd.Series) -> int:
    """Count how many leading values (most-recent first) are positive."""
    count = 0
    for v in series:
        if v > 0:
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# Analyser
# ---------------------------------------------------------------------------

class FundamentalAnalyzer:
    """
    Scores a stock's financial health from EDGAR data and a current price.

    Parameters
    ----------
    thresholds:
        Override any of the ``DEFAULT_THRESHOLDS`` keys to customise the
        pass/fail criteria.
    """

    def __init__(self, thresholds: Optional[Dict[str, float]] = None):
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    # ------------------------------------------------------------------
    def analyze(
        self,
        fundamentals: Dict,
        current_price: float,
        shares_outstanding: Optional[float] = None,
        dividend_yield: Optional[float]     = None,
    ) -> FundamentalScore:
        """
        Run the full fundamental analysis pipeline.

        Parameters
        ----------
        fundamentals:
            Dict returned by ``EdgarFetcher.fetch_fundamentals()``.
        current_price:
            Latest stock price (e.g. from ``PriceFetcher.get_current_price()``).
        shares_outstanding:
            Preferred source (yfinance) – used instead of EDGAR if provided.
        dividend_yield:
            Trailing 12-month yield from yfinance (0.0 for non-payers).
        """
        ticker = fundamentals["ticker"]
        t      = self.thresholds
        score  = FundamentalScore(ticker=ticker, current_price=current_price)
        checks: Dict[str, bool] = {}

        # Unpack annual series (may be empty)
        revenue     = fundamentals.get("revenue",             pd.Series(dtype=float))
        net_income  = fundamentals.get("net_income",          pd.Series(dtype=float))
        total_liab  = fundamentals.get("total_liabilities",   pd.Series(dtype=float))
        equity      = fundamentals.get("equity",              pd.Series(dtype=float))
        cur_assets  = fundamentals.get("current_assets",      pd.Series(dtype=float))
        cur_liab    = fundamentals.get("current_liabilities",  pd.Series(dtype=float))
        shares_edg  = fundamentals.get("shares_outstanding",  pd.Series(dtype=float))
        eps_series  = fundamentals.get("eps",                 pd.Series(dtype=float))

        # Resolve shares: prefer yfinance value, fall back to EDGAR
        shares = shares_outstanding
        if not shares and not shares_edg.empty:
            shares = float(shares_edg.iloc[0])

        # ── P/E Ratio ─────────────────────────────────────────────────
        pe = None
        if not eps_series.empty and eps_series.iloc[0] > 0:
            pe = current_price / eps_series.iloc[0]
        elif not net_income.empty and shares and shares > 0:
            eps = net_income.iloc[0] / shares
            if eps > 0:
                pe = current_price / eps

        score.pe_ratio = pe
        if pe is not None:
            checks["pe"] = pe <= t["pe_max"]

        # ── P/B Ratio ─────────────────────────────────────────────────
        pb = None
        if not equity.empty and shares and shares > 0:
            bvps = equity.iloc[0] / shares          # book value per share
            if bvps > 0:
                pb = current_price / bvps
        score.pb_ratio = pb
        if pb is not None:
            checks["pb"] = pb <= t["pb_max"]

        # ── Debt / Equity ─────────────────────────────────────────────
        de = None
        if not total_liab.empty and not equity.empty:
            eq = equity.iloc[0]
            if eq > 0:
                de = total_liab.iloc[0] / eq
        score.debt_equity = de
        if de is not None:
            checks["de"] = de <= t["de_max"]

        # ── Return on Equity (ROE) ────────────────────────────────────
        roe = None
        if not net_income.empty and not equity.empty:
            avg_eq = equity.iloc[:2].mean() if len(equity) >= 2 else equity.iloc[0]
            if avg_eq > 0:
                roe = net_income.iloc[0] / avg_eq
        score.roe = roe
        if roe is not None:
            checks["roe"] = roe >= t["roe_min"]

        # ── Net Profit Margin ─────────────────────────────────────────
        npm = None
        if not net_income.empty and not revenue.empty and revenue.iloc[0] > 0:
            npm = net_income.iloc[0] / revenue.iloc[0]
        score.net_profit_margin = npm
        if npm is not None:
            checks["npm"] = npm >= t["npm_min"]

        # ── Current Ratio ─────────────────────────────────────────────
        cr = None
        if not cur_assets.empty and not cur_liab.empty and cur_liab.iloc[0] > 0:
            cr = cur_assets.iloc[0] / cur_liab.iloc[0]
        score.current_ratio = cr
        if cr is not None:
            checks["current_ratio"] = cr >= t["current_ratio_min"]

        # ── Revenue CAGR (5-year) ─────────────────────────────────────
        rev_cagr = _cagr(revenue, years=5)
        score.revenue_cagr_5y = rev_cagr
        if rev_cagr is not None:
            checks["revenue_cagr"] = rev_cagr >= t["revenue_cagr_min"]

        # ── Net Income CAGR (5-year) — informational only ─────────────
        score.net_income_cagr_5y = _cagr(net_income, years=5)

        # ── Dividend Yield ────────────────────────────────────────────
        dy = float(dividend_yield or 0.0)
        score.dividend_yield = dy
        if dy > 0:
            checks["div_yield"] = dy >= t["div_yield_min"]

        # ── Consecutive profitable years ──────────────────────────────
        score.consecutive_profit_yrs = _consecutive_positive(net_income)

        # ── Aggregate score (0–100) ───────────────────────────────────
        total_w  = sum(WEIGHTS[k] for k in checks)
        passed_w = sum(WEIGHTS[k] for k, v in checks.items() if v)
        score.score   = (passed_w / total_w * 100.0) if total_w else 50.0
        score.checks  = checks

        # ── Signal label ──────────────────────────────────────────────
        s = score.score
        if s >= 75:
            score.signal_strength = "STRONG_BUY"
        elif s >= 60:
            score.signal_strength = "BUY"
        elif s >= 40:
            score.signal_strength = "HOLD"
        elif s >= 25:
            score.signal_strength = "SELL"
        else:
            score.signal_strength = "STRONG_SELL"

        return score
