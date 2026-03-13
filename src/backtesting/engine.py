"""
Backtesting Engine
===================
Walk-forward historical simulation of the GMR Short-Term trading strategy.

How it works
------------
1. The full price history is split into a *warm-up period* (the first 6 months,
   needed to bootstrap the GMR window) and a *trading period*.
2. At each monthly evaluation point the GMR indicator is run on the data
   available *up to that date only* — no look-ahead bias.
3. Positions are binary: either fully invested (100 % of capital in the stock)
   or 100 % in cash.  Fractional shares are allowed for simplicity.
4. A buy-and-hold benchmark is computed over the same period for comparison.
5. Performance metrics returned:
     total_return, annualised_return, benchmark_annualised_return,
     alpha, max_drawdown, Sharpe ratio, win_rate, num_trades.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import numpy as np
import pandas as pd

from ..analysis.technical import TechnicalAnalyzer

logger = logging.getLogger(__name__)

RISK_FREE_RATE = 0.04   # annualised, used for Sharpe calculation


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """A single open or close of a position."""

    date:         datetime
    action:       str       # "BUY" | "SELL"
    price:        float
    shares:       float
    value:        float
    signal_score: float
    reason:       str = ""


@dataclass
class BacktestResults:  # pylint: disable=too-many-instance-attributes
    """All outputs from one backtest run."""

    ticker:          str
    strategy:        str
    start_date:      datetime
    end_date:        datetime
    initial_capital: float

    trades:          List[Trade]  = field(default_factory=list)
    equity_curve:    pd.Series    = field(default_factory=pd.Series)
    benchmark_curve: pd.Series    = field(default_factory=pd.Series)

    # Performance metrics
    total_return:          float = 0.0
    annualised_return:     float = 0.0
    benchmark_ann_return:  float = 0.0
    alpha:                 float = 0.0
    max_drawdown:          float = 0.0
    sharpe_ratio:          float = 0.0
    win_rate:              float = 0.0
    num_trades:            int   = 0

    # ------------------------------------------------------------------
    def summary(self) -> str:
        """Return a human-readable summary of backtest results."""
        years = max((self.end_date - self.start_date).days / 365.25, 0.01)
        final = self.initial_capital * (1.0 + self.total_return)
        lines = [
            "",
            "=" * 62,
            f"  BACKTEST  |  {self.ticker}  |  Strategy: {self.strategy}",
            "=" * 62,
            f"  Period          {self.start_date.date()} → "
            f"{self.end_date.date()}  ({years:.1f} yrs)",
            f"  Initial capital ${self.initial_capital:>12,.2f}",
            f"  Final capital   ${final:>12,.2f}",
            "─" * 62,
            f"  Total return           {self.total_return * 100:>+8.2f} %",
            f"  Annualised return      {self.annualised_return * 100:>+8.2f} %",
            f"  Benchmark (ann.)       {self.benchmark_ann_return * 100:>+8.2f} %",
            f"  Alpha                  {self.alpha * 100:>+8.2f} %",
            f"  Max drawdown           {self.max_drawdown * 100:>8.2f} %",
            f"  Sharpe ratio           {self.sharpe_ratio:>8.2f}",
            f"  Number of trades       {self.num_trades:>8d}",
            f"  Win rate               {self.win_rate * 100:>8.1f} %",
            "=" * 62,
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd   = (equity - peak) / peak
    return float(dd.min())


def _annualised_sharpe(returns: pd.Series) -> float:
    if returns.empty or returns.std() == 0:
        return 0.0
    excess = returns - RISK_FREE_RATE / 252
    return float(np.sqrt(252) * excess.mean() / excess.std())


def _ann_return(total: float, days: float) -> float:
    years = max(days / 365.25, 0.01)
    return (1.0 + total) ** (1.0 / years) - 1.0


# ---------------------------------------------------------------------------
# GMR Technical backtester
# ---------------------------------------------------------------------------

class TechnicalBacktester:  # pylint: disable=too-few-public-methods
    """
    Walks forward through *price_history* one month at a time, running the
    GMR Short-Term indicator at each step and executing trades accordingly.

    Parameters
    ----------
    initial_capital:
        Starting cash in the simulation (any currency, default $10 000).
    rebalance_freq:
        Pandas offset alias for evaluation frequency.  Default ``"MS"``
        (month start).  Use ``"W"`` for weekly or ``"2W"`` for fortnightly.
    """

    def __init__(
        self,
        initial_capital: float = 10_000.0,
        rebalance_freq:  str   = "MS",
    ):
        self.initial_capital = initial_capital
        self.rebalance_freq  = rebalance_freq
        self._analyzer       = TechnicalAnalyzer()

    # ------------------------------------------------------------------
    def run(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self,
        ticker:        str,
        price_history: pd.DataFrame,
        start_date:    Optional[datetime] = None,
        end_date:      Optional[datetime] = None,
    ) -> BacktestResults:
        """
        Execute the backtest.

        Parameters
        ----------
        ticker:
            Stock symbol label.
        price_history:
            Daily OHLCV DataFrame covering at least 6 months *before*
            *start_date* (the warm-up window).  Must have a tz-naive
            DatetimeIndex.
        start_date:
            First date trades can be made.  Defaults to 6 months after the
            earliest row in *price_history*.
        end_date:
            Last date trades can be made.  Defaults to the last row.
        """
        # Ensure tz-naive index
        hist = price_history.copy()
        if hist.index.tz is not None:
            hist.index = hist.index.tz_localize(None)

        first = hist.index[0]
        last  = hist.index[-1]

        if end_date is None:
            end_date = last.to_pydatetime()
        if start_date is None:
            # Need ≥6 months warm-up before we start trading
            start_date = (first + pd.DateOffset(months=6)).to_pydatetime()

        start_ts = pd.Timestamp(start_date).normalize()
        end_ts   = pd.Timestamp(end_date).normalize()

        results = BacktestResults(
            ticker=ticker,
            strategy="GMR Short-Term",
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
        )

        cash         = self.initial_capital
        shares_held  = 0.0
        in_position  = False
        trades: List[Trade] = []

        # Monthly evaluation dates within the trading window
        eval_dates = pd.date_range(
            start=start_ts,
            end=end_ts,
            freq=self.rebalance_freq,
            normalize=True,
        )

        equity_vals: dict = {}

        for eval_ts in eval_dates:
            # Get the most recent price on or before this date
            available = hist[hist.index <= eval_ts]
            if available.empty:
                continue

            cur_price  = float(available["Close"].iloc[-1])
            portfolio  = cash + shares_held * cur_price
            equity_vals[eval_ts] = portfolio

            # Run GMR on data available up to (and including) eval_ts
            try:
                tech = self._analyzer.analyze(
                    price_history=hist[hist.index <= eval_ts],
                    ticker=ticker,
                    analysis_date=eval_ts.to_pydatetime(),
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.debug("GMR analysis failed at %s: %s", eval_ts.date(), exc)
                continue

            sig = tech.signal_strength

            if sig in ("BUY", "STRONG_BUY") and not in_position:
                # Enter long position — deploy all cash
                shares_held = cash / cur_price
                cash        = 0.0
                in_position = True
                trades.append(Trade(
                    date=eval_ts.to_pydatetime(),
                    action="BUY",
                    price=cur_price,
                    shares=shares_held,
                    value=shares_held * cur_price,
                    signal_score=tech.score,
                    reason=sig,
                ))
                logger.debug("BUY  %s @ $%.2f  score=%.0f", eval_ts.date(), cur_price, tech.score)

            elif sig in ("SELL", "STRONG_SELL") and in_position:
                # Exit position — convert all shares to cash
                cash        = shares_held * cur_price
                trades.append(Trade(
                    date=eval_ts.to_pydatetime(),
                    action="SELL",
                    price=cur_price,
                    shares=shares_held,
                    value=cash,
                    signal_score=tech.score,
                    reason=sig,
                ))
                shares_held = 0.0
                in_position = False
                logger.debug("SELL %s @ $%.2f  score=%.0f", eval_ts.date(), cur_price, tech.score)

        # ── Final portfolio value ──────────────────────────────────────
        final_avail = hist[hist.index <= end_ts]
        final_price = float(final_avail["Close"].iloc[-1]) if not final_avail.empty else 0.0
        final_value = cash + shares_held * final_price

        if not equity_vals:
            return results

        equity_series = pd.Series(equity_vals).sort_index()

        # ── Buy-and-hold benchmark ─────────────────────────────────────
        bh_data = hist[(hist.index >= start_ts) & (hist.index <= end_ts)]
        if not bh_data.empty:
            bh_start_price = float(bh_data["Close"].iloc[0])
            bh_shares      = self.initial_capital / bh_start_price
            benchmark_series = (bh_data["Close"] * bh_shares).rename("benchmark")
        else:
            benchmark_series = pd.Series(dtype=float)

        # ── Performance metrics ───────────────────────────────────────
        total_ret = (final_value - self.initial_capital) / self.initial_capital
        days      = (end_date - start_date).days
        ann_ret   = _ann_return(total_ret, days)

        if not benchmark_series.empty:
            bh_total = (
                (benchmark_series.iloc[-1] - benchmark_series.iloc[0])
                / benchmark_series.iloc[0]
            )
            bh_ann   = _ann_return(bh_total, days)
        else:
            bh_ann = 0.0

        # Daily returns from the equity curve (monthly points → reindex)
        monthly_rets  = equity_series.pct_change().dropna()
        sharpe        = _annualised_sharpe(monthly_rets)
        max_dd        = _max_drawdown(equity_series)

        # Win rate from completed buy/sell pairs
        buys  = [t for t in trades if t.action == "BUY"]
        sells = [t for t in trades if t.action == "SELL"]
        pairs = min(len(buys), len(sells))
        wins  = sum(sells[i].price > buys[i].price for i in range(pairs))
        win_rate = wins / pairs if pairs else 0.0

        results.trades               = trades
        results.equity_curve         = equity_series
        results.benchmark_curve      = benchmark_series
        results.total_return         = total_ret
        results.annualised_return    = ann_ret
        results.benchmark_ann_return = bh_ann
        results.alpha                = ann_ret - bh_ann
        results.max_drawdown         = max_dd
        results.sharpe_ratio         = sharpe
        results.win_rate             = win_rate
        results.num_trades           = len(trades)

        return results
