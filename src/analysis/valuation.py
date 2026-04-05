"""
Enterprise Valuation Analysis
==============================
Computes enterprise-value-based and capital-efficiency metrics for a given
ticker over a configurable historical window.

Metrics computed per fiscal year
---------------------------------
  EBITDA / EBITDA Margin
    EBITDA        = Operating Income + Depreciation & Amortization
    EBITDA Margin = EBITDA / Revenue × 100  [%]

  Leverage
    Net Debt           = Long-term Debt − Cash & Cash Equivalents
    Net Debt / EBITDA  = Net Debt / EBITDA

  Debt Serviceability
    Interest Coverage  = Operating Income (EBIT) / Interest Expense

  Capital Efficiency
    Effective Tax Rate = Income Tax Expense / (Net Income + Income Tax Expense) × 100  [%]
    NOPAT              = Operating Income × (1 − Effective Tax Rate)
    Invested Capital   = Equity + Long-term Debt − Cash & Cash Equivalents
    ROIC               = NOPAT / Invested Capital × 100  [%]

Enterprise Value metrics (current snapshot + most-recent-year EDGAR data)
---------------------------------------------------------------------------
    Enterprise Value   = Market Cap + Net Debt (most recent year)
    EV / EBITDA
    EV / Revenue
    EV / FCF
    EV / EBIT

Averages are taken across all fiscal years in the look-back window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .gmr_data_source import FinancialDataSource, MarketSnapshot

logger = logging.getLogger(__name__)

_DEFAULT_TAX_RATE = 0.21  # US statutory rate used when tax data is unavailable


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ValuationResult:  # pylint: disable=too-many-instance-attributes
    """Structured output of the enterprise valuation analysis."""
    ticker: str

    # Per-year table; index = fiscal year (descending)
    per_year: pd.DataFrame

    # ── Averages over the look-back window ──────────────────────────────
    avg_ebitda_margin: float        # %
    avg_roic: float                 # %
    avg_interest_coverage: float
    avg_net_debt_to_ebitda: float

    # ── Current enterprise-value metrics ────────────────────────────────
    enterprise_value: float
    ev_ebitda: float
    ev_revenue: float
    ev_fcf: float
    ev_ebit: float

    # ── Market context (for reference) ──────────────────────────────────
    current_price: float
    market_cap: float
    last_dividend: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class Valuation:  # pylint: disable=too-few-public-methods
    """
    Computes enterprise-level valuation and capital-efficiency metrics.

    Example::

        ds = LiveDataSource()
        result = Valuation(ds).compute("AAPL", years=5)
        print(result.ev_ebitda, result.avg_roic)
    """

    def __init__(self, data_source: FinancialDataSource) -> None:
        self._ds = data_source

    # ------------------------------------------------------------------
    def compute(  # pylint: disable=too-many-locals,too-many-statements
        self, ticker: str, years: int = 5
    ) -> ValuationResult:
        """Compute enterprise valuation metrics for *ticker* over *years* fiscal years."""
        logger.info("Computing valuation for %s (%d years)…", ticker, years)
        fundamentals  = self._ds.get_annual_fundamentals(ticker, years)
        annual_prices = self._ds.get_annual_avg_prices(ticker, years)
        snapshot      = self._ds.get_market_snapshot(ticker)

        # ── Unpack series ───────────────────────────────────────────────
        operating_income = fundamentals.get("operating_income",          pd.Series(dtype=float))
        revenue          = fundamentals.get("revenue",                   pd.Series(dtype=float))
        fcf              = fundamentals.get("free_cashflow",             pd.Series(dtype=float))
        equity           = fundamentals.get("equity",                    pd.Series(dtype=float))
        long_term_debt   = fundamentals.get("long_term_debt",            pd.Series(dtype=float))
        cash             = fundamentals.get("cash_and_equivalents",      pd.Series(dtype=float))
        da               = fundamentals.get("depreciation_amortization", pd.Series(dtype=float))
        interest_exp     = fundamentals.get("interest_expense",          pd.Series(dtype=float))
        income_tax       = fundamentals.get("income_tax_expense",        pd.Series(dtype=float))
        net_income       = fundamentals.get("net_income",                pd.Series(dtype=float))

        # ── Determine fiscal years ──────────────────────────────────────
        all_series = [operating_income, revenue, equity, long_term_debt,
                      cash, da, interest_exp, annual_prices]
        all_years: set = set()
        for s in all_series:
            if not s.empty:
                all_years.update(int(y) for y in s.index)

        sorted_years = sorted(all_years, reverse=True)[:years]

        if not sorted_years:
            logger.warning("No fiscal years found for '%s'", ticker)
            return self._empty_result(ticker, snapshot)

        logger.info(
            "Valuation for %s: %d fiscal years [%s]",
            ticker, len(sorted_years), ", ".join(str(y) for y in sorted_years),
        )

        # ── Helpers ─────────────────────────────────────────────────────
        nan = float("nan")

        def _v(series: pd.Series, yr: int, default: float = nan) -> float:
            if hasattr(series, "at") and yr in series.index:
                val = series.at[yr]
                return default if val is None else float(val)
            return default

        def _safe(num: float, denom: float) -> float:
            if (num and not np.isnan(num) and
                    denom and not np.isnan(denom) and denom != 0):
                return num / denom
            return nan

        # ── Build per-year rows ─────────────────────────────────────────
        rows = []
        for yr in sorted_years:
            op_inc  = _v(operating_income, yr)
            rev     = _v(revenue,          yr)
            eq      = _v(equity,           yr)
            ltd     = _v(long_term_debt,   yr, 0.0)
            cash_v  = _v(cash,             yr, 0.0)
            da_v    = _v(da,               yr, 0.0)
            int_exp = _v(interest_exp,     yr)
            tax_v   = _v(income_tax,       yr)
            ni      = _v(net_income,       yr)

            # EBITDA
            ebitda = op_inc + da_v if (not np.isnan(op_inc) and da_v is not None) else nan
            ebitda_margin = _safe(ebitda, rev) * 100 if not np.isnan(_safe(ebitda, rev)) else nan

            # Net Debt = Long-term Debt − Cash
            net_debt = ltd - cash_v if not np.isnan(ltd) else nan
            net_debt_ebitda = _safe(net_debt, ebitda) if (
                not np.isnan(net_debt) and not np.isnan(ebitda)
            ) else nan

            # Interest Coverage = EBIT / Interest Expense
            interest_coverage = _safe(op_inc, int_exp) if (
                not np.isnan(int_exp) and int_exp > 0
            ) else nan

            # ROIC
            # Effective tax rate = tax / (NI + tax), clamped to [0, 0.5]
            if (not np.isnan(tax_v) and not np.isnan(ni) and
                    (ni + tax_v) != 0 and not np.isnan(ni + tax_v)):
                eff_tax = max(0.0, min(0.5, tax_v / (ni + tax_v)))
            else:
                eff_tax = _DEFAULT_TAX_RATE

            nopat = op_inc * (1.0 - eff_tax) if not np.isnan(op_inc) else nan

            # Invested Capital = Equity + Long-term Debt − Cash
            if not np.isnan(eq) and not np.isnan(ltd):
                invested_capital = eq + ltd - cash_v
            else:
                invested_capital = nan

            roic = _safe(nopat, invested_capital) * 100 if (
                not np.isnan(_safe(nopat, invested_capital))
            ) else nan

            rows.append({
                "year":               yr,
                "da":                 da_v if abs(da_v) > 1e-9 else nan,
                "interest_expense":   int_exp,
                "cash_and_equivalents": cash_v if abs(cash_v) > 1e-9 else nan,
                "long_term_debt":     ltd if abs(ltd) > 1e-9 else nan,
                "ebitda":             ebitda,
                "ebitda_margin":      ebitda_margin,
                "net_debt":           net_debt,
                "net_debt_to_ebitda": net_debt_ebitda,
                "interest_coverage":  interest_coverage,
                "effective_tax_rate": eff_tax * 100,
                "nopat":              nopat,
                "invested_capital":   invested_capital,
                "roic":               roic,
            })

        per_year = pd.DataFrame(rows).set_index("year")

        # ── Averages ────────────────────────────────────────────────────
        def _avg(col: str) -> float:
            if col not in per_year.columns:
                return nan
            vals = per_year[col].replace([np.inf, -np.inf], np.nan).dropna()
            return float(vals.mean()) if not vals.empty else nan

        # ── Enterprise Value (current market cap + most-recent net debt) ─
        current_price = float(snapshot.current_price or nan)
        snap_shares   = float(snapshot.shares_outstanding or 0)
        market_cap    = (current_price * snap_shares
                         if not np.isnan(current_price) and snap_shares > 0
                         else nan)

        # Prefer EDGAR most-recent-year net debt for EV calculation
        most_recent_yr = sorted_years[0] if sorted_years else None
        if most_recent_yr is not None:
            ltd_curr  = _v(long_term_debt, most_recent_yr, 0.0)
            cash_curr = _v(cash,           most_recent_yr, 0.0)
            net_debt_current = ltd_curr - cash_curr
        else:
            net_debt_current = nan

        enterprise_value = (market_cap + net_debt_current
                            if not np.isnan(market_cap) and not np.isnan(net_debt_current)
                            else nan)

        # EV multiples use the most-recent fiscal year's figures
        def _ev_multiple(denominator: float) -> float:
            return _safe(enterprise_value, denominator)

        ebitda_series = per_year["ebitda"].dropna()
        latest_ebitda = float(ebitda_series.iloc[0]) if not ebitda_series.empty else nan
        latest_revenue  = _v(revenue,  most_recent_yr) if most_recent_yr else nan
        latest_fcf      = _v(fcf,      most_recent_yr) if most_recent_yr else nan
        latest_ebit     = _v(operating_income, most_recent_yr) if most_recent_yr else nan

        return ValuationResult(
            ticker=ticker.upper(),
            per_year=per_year,
            avg_ebitda_margin=_avg("ebitda_margin"),
            avg_roic=_avg("roic"),
            avg_interest_coverage=_avg("interest_coverage"),
            avg_net_debt_to_ebitda=_avg("net_debt_to_ebitda"),
            enterprise_value=enterprise_value,
            ev_ebitda=_ev_multiple(latest_ebitda),
            ev_revenue=_ev_multiple(latest_revenue),
            ev_fcf=_ev_multiple(latest_fcf),
            ev_ebit=_ev_multiple(latest_ebit),
            current_price=current_price,
            market_cap=market_cap,
            last_dividend={
                "date": snapshot.last_dividend_date,
                "amount": snapshot.last_dividend_amount,
            },
        )

    # ------------------------------------------------------------------
    def _empty_result(self, ticker: str, snapshot: MarketSnapshot) -> ValuationResult:
        nan = float("nan")
        current_price = float(snapshot.current_price or nan)
        snap_shares   = float(snapshot.shares_outstanding or 0)
        market_cap    = (current_price * snap_shares
                         if not np.isnan(current_price) and snap_shares > 0
                         else nan)
        return ValuationResult(
            ticker=ticker.upper(),
            per_year=pd.DataFrame(),
            avg_ebitda_margin=nan,
            avg_roic=nan,
            avg_interest_coverage=nan,
            avg_net_debt_to_ebitda=nan,
            enterprise_value=nan,
            ev_ebitda=nan,
            ev_revenue=nan,
            ev_fcf=nan,
            ev_ebit=nan,
            current_price=current_price,
            market_cap=market_cap,
        )
