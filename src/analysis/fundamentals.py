"""
Financial Fundamentals Analysis
=================================
Computes a consensus set of fundamental financial metrics for a given ticker
over a configurable historical window.

Metrics computed per fiscal year
---------------------------------
  Valuation
    P/E   = avg_annual_price / (net_income / shares)
    P/B   = avg_annual_price / (equity / shares)
    P/S   = avg_annual_price / (revenue / shares)

  Profitability
    ROE          = net_income / equity × 100  [%]
    ROA          = net_income / total_assets × 100  [%]
    NPM          = net_income / revenue × 100  [%]
    Gross Margin = gross_profit / revenue × 100  [%]
    Op. Margin   = operating_income / revenue × 100  [%]

  Liquidity / Leverage
    Current Ratio  = current_assets / current_liabilities
    Quick Ratio    = (current_assets − inventory − prepaid) / current_liabilities
    Debt/Equity    = total_liabilities / equity
    Debt/Assets    = total_liabilities / total_assets

  Cash Flow
    FCF Yield      = (free_cashflow / shares) / avg_price × 100  [%]
    Dividend Yield = annual_dividends / avg_price × 100  [%]

  Growth (year-over-year)
    Revenue Growth  = (rev_t − rev_t−1) / |rev_t−1| × 100  [%]
    Earnings Growth = (ni_t − ni_t−1) / |ni_t−1| × 100  [%]

  Per-share
    EPS, Book Value/Share, Revenue/Share, FCF/Share, Dividend/Share

Averages are taken across all fiscal years in the look-back window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .gmr_data_source import FinancialDataSource

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class FundamentalsResult:  # pylint: disable=too-many-instance-attributes
    """Structured output of the fundamentals analysis."""
    ticker: str

    # Per-year table; index = fiscal year (descending), columns = all metrics
    per_year: pd.DataFrame

    # ── Averages over the look-back window ──────────────────────────────
    # Valuation
    avg_pe: float
    avg_pb: float
    avg_ps: float
    # Profitability
    avg_roe: float            # %
    avg_roa: float            # %
    avg_npm: float            # %
    avg_gross_margin: float   # %
    avg_operating_margin: float  # %
    # Liquidity / Leverage
    avg_current_ratio: float
    avg_quick_ratio: float
    avg_debt_to_equity: float
    avg_debt_to_assets: float
    # Cash flow
    avg_fcf_yield: float      # %
    avg_dividend_yield: float  # %
    # Growth
    avg_revenue_growth: float    # %
    avg_earnings_growth: float   # %

    # ── Market context ───────────────────────────────────────────────────
    current_price: float
    market_cap: float
    shares_outstanding: float
    avg_volume: float
    last_dividend: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class Fundamentals:  # pylint: disable=too-few-public-methods
    """
    Computes a comprehensive set of financial fundamentals for a ticker.

    Example::

        ds = LiveDataSource()        # or a mock
        result = Fundamentals(ds).compute("AAPL", years=5)
        print(result.avg_roe, result.per_year)
    """

    def __init__(self, data_source: FinancialDataSource) -> None:
        self._ds = data_source

    # ------------------------------------------------------------------
    def compute(  # pylint: disable=too-many-locals,too-many-statements
        self, ticker: str, years: int = 5
    ) -> FundamentalsResult:
        """Compute fundamentals for *ticker* over the last *years* fiscal years."""
        logger.info("Computing fundamentals for %s (%d years)…", ticker, years)
        fundamentals  = self._ds.get_annual_fundamentals(ticker, years)
        annual_prices = self._ds.get_annual_avg_prices(ticker, years)
        dividends     = self._ds.get_annual_dividends(ticker)
        snapshot      = self._ds.get_market_snapshot(ticker)

        # Unpack — each value is a pd.Series indexed by int fiscal year
        revenue          = fundamentals.get("revenue",            pd.Series(dtype=float))
        gross_profit     = fundamentals.get("gross_profit",       pd.Series(dtype=float))
        operating_income = fundamentals.get("operating_income",   pd.Series(dtype=float))
        net_income       = fundamentals.get("net_income",         pd.Series(dtype=float))
        total_assets     = fundamentals.get("total_assets",       pd.Series(dtype=float))
        total_liab       = fundamentals.get("total_liabilities",  pd.Series(dtype=float))
        equity           = fundamentals.get("equity",             pd.Series(dtype=float))
        current_assets   = fundamentals.get("current_assets",     pd.Series(dtype=float))
        current_liabs    = fundamentals.get("current_liabilities",pd.Series(dtype=float))
        inventory        = fundamentals.get("inventory",          pd.Series(dtype=float))
        prepaid          = fundamentals.get("prepaid_expenses",   pd.Series(dtype=float))
        shares           = fundamentals.get("shares_outstanding", pd.Series(dtype=float))
        fcf              = fundamentals.get("free_cashflow",      pd.Series(dtype=float))
        operating_cf     = fundamentals.get("operating_cashflow", pd.Series(dtype=float))
        capex            = fundamentals.get("capex",              pd.Series(dtype=float))

        # ── Determine fiscal years (union — include any year with any data) ──
        all_series = [revenue, net_income, equity, shares, annual_prices,
                      total_assets, total_liab, current_assets, current_liabs]
        all_years: set = set()
        for s in all_series:
            if not s.empty:
                all_years.update(int(y) for y in s.index)

        sorted_years = sorted(all_years, reverse=True)[:years]

        if not sorted_years:
            logger.warning("No fiscal years found for '%s' — returning empty result", ticker)
            return self._empty_result(ticker, snapshot)

        logger.info(
            "Fundamentals for %s: %d fiscal years [%s]",
            ticker,
            len(sorted_years),
            ", ".join(str(y) for y in sorted_years),
        )

        # ── Safe scalar lookup ────────────────────────────────────────────
        nan = float("nan")

        def _v(series: pd.Series, yr: int, default: float = nan) -> float:
            if hasattr(series, "at") and yr in series.index:
                return float(series.at[yr])
            return default

        def _safe(num: float, denom: float) -> float:
            if denom and not np.isnan(denom) and denom != 0 and num and not np.isnan(num):
                return num / denom
            return nan

        def _price_ratio(price: float, divisor: float) -> float:
            if np.isnan(price) or np.isnan(divisor) or divisor <= 0:
                return nan
            return price / divisor

        # ── Build per-year rows ───────────────────────────────────────────
        rows = []
        for yr in sorted_years:
            price  = _v(annual_prices, yr)
            rev    = _v(revenue,          yr)
            gp     = _v(gross_profit,     yr)
            op_inc = _v(operating_income, yr)
            ni     = _v(net_income,       yr)
            assets = _v(total_assets,     yr)
            liab   = _v(total_liab,       yr)
            eq     = _v(equity,           yr)
            ca     = _v(current_assets,   yr)
            cl     = _v(current_liabs,    yr)
            inv    = _v(inventory,        yr, 0.0)
            prep   = _v(prepaid,          yr, 0.0)
            sh     = _v(shares,           yr)
            fcf_v  = _v(fcf,              yr)
            div    = _v(dividends,        yr, 0.0)

            eps_v  = _safe(ni, sh)
            bvps   = _safe(eq, sh)
            rev_ps = _safe(rev, sh)
            fcf_ps = _safe(fcf_v, sh)

            pe = _price_ratio(price, eps_v)
            pb = _price_ratio(price, bvps)
            ps = _price_ratio(price, rev_ps)

            roe      = _safe(ni,     eq)     * 100 if not np.isnan(_safe(ni, eq))     else nan
            roa      = _safe(ni,     assets) * 100 if not np.isnan(_safe(ni, assets)) else nan
            npm      = _safe(ni,     rev)    * 100 if not np.isnan(_safe(ni, rev))    else nan
            gm       = _safe(gp,     rev)    * 100 if not np.isnan(_safe(gp, rev))    else nan
            om       = _safe(op_inc, rev)    * 100 if not np.isnan(_safe(op_inc, rev)) else nan

            cr       = _safe(ca, cl)
            qr       = _safe(ca - inv - prep, cl) if (cl and not np.isnan(cl) and cl != 0) else nan
            de       = _safe(liab, eq)
            da       = _safe(liab, assets)

            _fcf_ps_ratio = _safe(fcf_ps, price)
            fcf_yield = _fcf_ps_ratio * 100 if not np.isnan(_fcf_ps_ratio) else nan
            div_yield = div / price * 100 if (price and not np.isnan(price) and price > 0) else 0.0

            rows.append({
                "year":             yr,
                "avg_price":        price,
                # Income statement
                "revenue":          rev,
                "gross_profit":     gp,
                "operating_income": op_inc,
                "net_income":       ni,
                "eps":              eps_v,
                # Balance sheet
                "total_assets":     assets,
                "total_liabilities": liab,
                "equity":           eq,
                "shares":           sh,
                "current_assets":   ca,
                "current_liabilities": cl,
                # Cash flow
                "operating_cashflow": _v(operating_cf, yr),
                "capex":            _v(capex, yr),
                "free_cashflow":    fcf_v,
                # Per-share
                "book_value_per_share": bvps,
                "revenue_per_share":    rev_ps,
                "fcf_per_share":        fcf_ps,
                "dividend_per_share":   div,
                # Ratios
                "pe": pe, "pb": pb, "ps": ps,
                "roe": roe, "roa": roa, "npm": npm,
                "gross_margin": gm, "operating_margin": om,
                "current_ratio": cr, "quick_ratio": qr,
                "debt_to_equity": de, "debt_to_assets": da,
                "fcf_yield": fcf_yield, "dividend_yield": div_yield,
                # Growth placeholder (filled below)
                "revenue_growth": nan,
                "earnings_growth": nan,
            })

        per_year = pd.DataFrame(rows).set_index("year")

        # ── Compute YoY growth rates ──────────────────────────────────────
        # sorted_years is descending; year[i+1] is one year prior to year[i]
        for i, yr in enumerate(sorted_years):
            if i + 1 < len(sorted_years):
                prev_yr = sorted_years[i + 1]
                prev_rev = _v(revenue,    prev_yr)
                prev_ni  = _v(net_income, prev_yr)
                curr_rev = _v(revenue,    yr)
                curr_ni  = _v(net_income, yr)

                if not np.isnan(prev_rev) and prev_rev != 0 and not np.isnan(curr_rev):
                    per_year.at[yr, "revenue_growth"] = (curr_rev - prev_rev) / abs(prev_rev) * 100
                if not np.isnan(prev_ni) and prev_ni != 0 and not np.isnan(curr_ni):
                    per_year.at[yr, "earnings_growth"] = (curr_ni - prev_ni) / abs(prev_ni) * 100

        # ── Averages ──────────────────────────────────────────────────────
        def _avg(col: str) -> float:
            if col not in per_year.columns:
                return nan
            vals = per_year[col].dropna()
            return float(vals.mean()) if not vals.empty else nan

        # ── Market snapshot ───────────────────────────────────────────────
        current_price  = float(snapshot.get("current_price", nan))
        snap_shares    = float(snapshot.get("shares_outstanding") or 0)
        volume         = float(snapshot.get("avg_volume") or 0)
        market_cap     = current_price * snap_shares if (
            not np.isnan(current_price) and snap_shares > 0
        ) else nan

        return FundamentalsResult(
            ticker=ticker.upper(),
            per_year=per_year,
            # Valuation
            avg_pe=_avg("pe"),
            avg_pb=_avg("pb"),
            avg_ps=_avg("ps"),
            # Profitability
            avg_roe=_avg("roe"),
            avg_roa=_avg("roa"),
            avg_npm=_avg("npm"),
            avg_gross_margin=_avg("gross_margin"),
            avg_operating_margin=_avg("operating_margin"),
            # Liquidity / Leverage
            avg_current_ratio=_avg("current_ratio"),
            avg_quick_ratio=_avg("quick_ratio"),
            avg_debt_to_equity=_avg("debt_to_equity"),
            avg_debt_to_assets=_avg("debt_to_assets"),
            # Cash flow
            avg_fcf_yield=_avg("fcf_yield"),
            avg_dividend_yield=_avg("dividend_yield"),
            # Growth
            avg_revenue_growth=_avg("revenue_growth"),
            avg_earnings_growth=_avg("earnings_growth"),
            # Market
            current_price=current_price,
            market_cap=market_cap,
            shares_outstanding=snap_shares,
            avg_volume=volume,
            last_dividend=snapshot.get("last_dividend", {}),
        )

    # ------------------------------------------------------------------
    def _empty_result(self, ticker: str, snapshot: dict) -> FundamentalsResult:
        nan = float("nan")
        return FundamentalsResult(
            ticker=ticker.upper(),
            per_year=pd.DataFrame(),
            avg_pe=nan, avg_pb=nan, avg_ps=nan,
            avg_roe=nan, avg_roa=nan, avg_npm=nan,
            avg_gross_margin=nan, avg_operating_margin=nan,
            avg_current_ratio=nan, avg_quick_ratio=nan,
            avg_debt_to_equity=nan, avg_debt_to_assets=nan,
            avg_fcf_yield=nan, avg_dividend_yield=nan,
            avg_revenue_growth=nan, avg_earnings_growth=nan,
            current_price=float(snapshot.get("current_price", nan)),
            market_cap=nan,
            shares_outstanding=float(snapshot.get("shares_outstanding") or 0),
            avg_volume=float(snapshot.get("avg_volume") or 0),
        )
