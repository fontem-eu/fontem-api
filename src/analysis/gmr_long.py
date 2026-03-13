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
from typing import Dict, Optional

import numpy as np
import pandas as pd

from .gmr_data_source import FinancialDataSource, GMRSettings

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
    flags: Dict[str, bool]
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
        settings: Optional[GMRSettings] = None,
    ) -> None:
        self._ds = data_source
        self._s = settings or GMRSettings()

    # ------------------------------------------------------------------
    def compute(  # pylint: disable=too-many-locals,too-many-statements
        self, ticker: str, years: Optional[int] = None
    ) -> GMRLongResult:
        """Run the long-term GMR screen and return a :class:`GMRLongResult`."""
        n = years if years is not None else self._s.years_for_avg

        fundamentals  = self._ds.get_annual_fundamentals(ticker, n)
        annual_prices = self._ds.get_annual_avg_prices(ticker, n)
        dividends     = self._ds.get_annual_dividends(ticker)
        snapshot      = self._ds.get_market_snapshot(ticker)

        # Unpack — each value is a pd.Series indexed by int year
        revenue        = fundamentals.get("revenue",            pd.Series(dtype=float))
        net_income     = fundamentals.get("net_income",         pd.Series(dtype=float))
        equity         = fundamentals.get("equity",             pd.Series(dtype=float))
        liabilities    = fundamentals.get("total_liabilities",  pd.Series(dtype=float))
        shares         = fundamentals.get("shares_outstanding", pd.Series(dtype=float))
        current_assets = fundamentals.get("current_assets",     pd.Series(dtype=float))
        current_liabs  = fundamentals.get("current_liabilities",pd.Series(dtype=float))
        inventory      = fundamentals.get("inventory",          pd.Series(dtype=float))
        prepaid        = fundamentals.get("prepaid_expenses",   pd.Series(dtype=float))
        fcf            = fundamentals.get("free_cashflow",      pd.Series(dtype=float))

        # ── Determine common fiscal years across essential series ─────────
        essential = [revenue, net_income, equity, liabilities, shares, annual_prices]
        common_years = essential[0].index
        for s in essential[1:]:
            if not s.empty:
                common_years = common_years.intersection(s.index)
        common_years = sorted(common_years, reverse=True)[:n]

        if not common_years:
            logger.warning("No common fiscal years for '%s' — returning empty result", ticker)
            return self._empty_result(ticker, snapshot)

        # ── Build per-year ratio table ───────────────────────────────────
        def _v(series: pd.Series, yr: int, default: float = float("nan")) -> float:
            """Safe scalar lookup."""
            return float(series.at[yr]) if yr in series.index else default

        rows = []
        for yr in common_years:
            price = _v(annual_prices, yr)
            rev   = _v(revenue,       yr)
            ni    = _v(net_income,    yr)
            eq    = _v(equity,        yr)
            liab  = _v(liabilities,   yr)
            sh    = _v(shares,        yr)
            ca    = _v(current_assets, yr)
            cl    = _v(current_liabs,  yr)
            inv   = _v(inventory,     yr, 0.0)
            prep  = _v(prepaid,       yr, 0.0)
            div   = _v(dividends,     yr, 0.0)
            fcf_v = _v(fcf,           yr)

            nan = float("nan")

            eps_v = ni / sh   if (sh  and sh  > 0) else nan
            bvps  = eq / sh   if (sh  and sh  > 0) else nan
            pe    = price / eps_v if (eps_v and eps_v > 0 and not np.isnan(price)) else nan
            pb    = price / bvps  if (bvps  and bvps  > 0 and not np.isnan(price)) else nan
            roe   = ni / eq * 100 if (eq    and eq    > 0) else nan
            npm   = ni / rev * 100 if (rev   and rev   > 0) else nan
            de    = liab / eq      if (eq    and eq    > 0) else nan
            dy    = div / price * 100 if (price and price > 0) else 0.0
            qr    = (ca - inv - prep) / cl if (cl and cl > 0) else nan

            rows.append({
                "year":             yr,
                "avg_price":        price,
                "revenue":          rev,
                "net_income":       ni,
                "equity":           eq,
                "total_liabilities": liab,
                "shares":           sh,
                "pe":               pe,
                "pb":               pb,
                "roe":              roe,
                "npm":              npm,
                "debt_equity":      de,
                "dividend_yield":   dy,
                "quick_ratio":      qr,
                "free_cashflow":    fcf_v,
                "dividends":        div,
            })

        per_year = pd.DataFrame(rows).set_index("year")

        # ── Average across years ─────────────────────────────────────────
        avg_pe  = float(np.nanmean(per_year["pe"]))
        avg_pb  = float(np.nanmean(per_year["pb"]))
        avg_roe = float(np.nanmean(per_year["roe"]))
        avg_npm = float(np.nanmean(per_year["npm"]))
        avg_de  = float(np.nanmean(per_year["debt_equity"]))
        avg_dy  = float(np.nanmean(per_year["dividend_yield"]))
        avg_qr  = float(np.nanmean(per_year["quick_ratio"]))
        avg_fcf = float(np.nanmean(per_year["free_cashflow"]))

        # ── Threshold checks ─────────────────────────────────────────────
        s = self._s
        flags: Dict[str, bool] = {
            "pe":             avg_pe  <= s.pe,
            "pb":             avg_pb  <= s.pb_value,
            "roe":            avg_roe >= s.roe,
            "npm":            avg_npm >= s.net_profit_margin,
            "debt_equity":    avg_de  <= s.debt_equity,
            "dividend_yield": avg_dy  >= s.dividend_yield * 100,
        }
        passes_all = all(flags.values())

        return GMRLongResult(
            ticker=ticker.upper(),
            per_year=per_year,
            avg_pe=avg_pe,
            avg_pb=avg_pb,
            avg_roe=avg_roe,
            avg_npm=avg_npm,
            avg_debt_equity=avg_de,
            avg_dividend_yield=avg_dy,
            avg_quick_ratio=avg_qr,
            avg_fcf=avg_fcf,
            flags=flags,
            passes_all=passes_all,
            current_price=float(snapshot.get("current_price", float("nan"))),
            avg_volume=float(snapshot.get("avg_volume", 0) or 0),
            last_dividend=snapshot.get("last_dividend", {}),
            splits=snapshot.get("splits", pd.Series(dtype=float)),
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
