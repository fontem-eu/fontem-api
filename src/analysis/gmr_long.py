"""
GMR Long-Term Value Investing Screen
======================================
Evaluates a stock across N fiscal years using the GMR multi-factor framework.
All I/O is delegated to an injected GMRDataSource — this module is pure logic.

Ratios computed per year
------------------------
  P/E   = avg_annual_price / (net_income / shares)
  P/B   = avg_annual_price / (equity / shares)
  ROE   = net_income / equity  × 100  [%]
  NPM   = net_income / revenue × 100  [%]
  D/E   = total_liabilities / equity
  DivY  = annual_dividends / avg_price × 100  [%]
  QR    = (current_assets − inventory − prepaid) / current_liabilities
  FCF   = free_cashflow  (pass-through, already computed by fetcher)

Averages are taken over the look-back window; each average is compared
against a threshold from GMRSettings.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .gmr_data_source import FinancialDataSource, GMRSettings
from .fundamentals import Fundamentals

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class GMRLongResult:  # pylint: disable=too-many-instance-attributes
    """Structured output of the GMR long-term screen."""
    ticker: str

    # Per-year table; index = fiscal year, columns = all computed ratios
    per_year: pd.DataFrame

    # Averages over the look-back window
    avg_pe: float
    avg_pb: float
    avg_roe: float           # %
    avg_npm: float           # %
    avg_debt_equity: float
    avg_dividend_yield: float  # %
    avg_quick_ratio: float
    avg_fcf: float

    # Boolean verdict per ratio
    flags: dict[str, bool]
    passes_all: bool

    # Market context (from snapshot)
    current_price: float
    avg_volume: float
    last_dividend: dict = field(default_factory=dict)
    splits: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class GMRLong:  # pylint: disable=too-few-public-methods
    """
    GMR Long-term screen.

    Example::

        ds = LiveDataSource(...)      # or MockDataSource(...)
        result = GMRLong(ds).compute("KO", years=5)
        print(result.passes_all, result.flags)
    """

    def __init__(
        self,
        data_source: FinancialDataSource,
        settings: GMRSettings | None = None,
    ) -> None:
        self._ds = data_source
        self._s = settings or GMRSettings()

    # ------------------------------------------------------------------
    def compute(
        self, ticker: str, years: int | None = None
    ) -> GMRLongResult:
        """Run the long-term GMR screen and return a :class:`GMRLongResult`."""
        n = years if years is not None else self._s.years_for_avg

        fund = Fundamentals(self._ds).compute(ticker, n)

        if fund.per_year.empty:
            logger.warning("No fiscal years found for '%s' — returning empty result", ticker)
            return self._empty_result(ticker, {
                "current_price": fund.current_price,
                "avg_volume":    fund.avg_volume,
            })

        # Add alias columns expected by the router
        per_year = fund.per_year.copy()
        per_year["debt_equity"] = per_year["debt_to_equity"]
        per_year["dividends"]   = per_year["dividend_per_share"]

        avg_fcf = float(np.nanmean(per_year["free_cashflow"]))

        s = self._s
        flags: dict[str, bool] = {
            "pe":             fund.avg_pe              <= s.pe,
            "pb":             fund.avg_pb              <= s.pb_value,
            "roe":            fund.avg_roe             >= s.roe,
            "npm":            fund.avg_npm             >= s.net_profit_margin,
            "debt_equity":    fund.avg_debt_to_equity  <= s.debt_equity,
            "dividend_yield": fund.avg_dividend_yield  >= s.dividend_yield * 100,
        }

        return GMRLongResult(
            ticker=ticker.upper(),
            per_year=per_year,
            avg_pe=fund.avg_pe,
            avg_pb=fund.avg_pb,
            avg_roe=fund.avg_roe,
            avg_npm=fund.avg_npm,
            avg_debt_equity=fund.avg_debt_to_equity,
            avg_dividend_yield=fund.avg_dividend_yield,
            avg_quick_ratio=fund.avg_quick_ratio,
            avg_fcf=avg_fcf,
            flags=flags,
            passes_all=all(flags.values()),
            current_price=fund.current_price,
            avg_volume=fund.avg_volume,
            last_dividend=fund.last_dividend,
            splits=pd.Series(dtype=float),
        )

    # ------------------------------------------------------------------
    def _empty_result(self, ticker: str, snapshot: dict) -> GMRLongResult:
        empty_flags = {k: False for k in
                       ("pe", "pb", "roe", "npm", "debt_equity", "dividend_yield")}
        return GMRLongResult(
            ticker=ticker.upper(),
            per_year=pd.DataFrame(),
            avg_pe=float("nan"), avg_pb=float("nan"),
            avg_roe=float("nan"), avg_npm=float("nan"),
            avg_debt_equity=float("nan"), avg_dividend_yield=float("nan"),
            avg_quick_ratio=float("nan"), avg_fcf=float("nan"),
            flags=empty_flags, passes_all=False,
            current_price=float(snapshot.get("current_price", float("nan"))),
            avg_volume=float(snapshot.get("avg_volume", 0) or 0),
        )
