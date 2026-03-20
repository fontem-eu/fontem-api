"""
Financial Fundamentals Endpoint
================================
GET /{ticker}/fundamentals

Returns a comprehensive set of financial fundamentals for a given ticker,
averaged over a configurable number of fiscal years.

Response includes:
  • market_snapshot  — current price, market cap, volume, last dividend
  • ratios_summary   — averages of all key ratios over the look-back window
  • per_year         — per-fiscal-year raw figures and computed ratios

Use ?years=N (default 5, range 1–20) to control the historical window.
Use ?summarize=true to receive only ticker + ratios_summary (no per_year table).

A 404 is returned when:
  • the ticker has no filings on EDGAR, or
  • the ticker is unknown / data source raises ValueError / LookupError.
"""
# The try/except 404 pattern is intentionally identical across all analysis
# routers — it is standard FastAPI error handling, not a shared abstraction.
# pylint: disable=duplicate-code
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from src.analysis.fundamentals import Fundamentals
from src.analysis.gmr_data_source import FinancialDataSource
from src.api.dependencies import get_data_source
from src.api.helpers import _f
from src.api.schemas.fundamentals import (
    FundamentalsMarketSnapshot,
    FundamentalsPerYearRow,
    FundamentalsRatiosSummary,
    FundamentalsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Fundamentals"])


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/{ticker}/fundamentals",
    response_model=FundamentalsResponse,
    response_model_exclude_none=True,
    summary="Financial Fundamentals",
    description=(
        "Returns a consensus set of financial fundamentals for a given ticker, "
        "covering **valuation** (P/E, P/B, P/S), **profitability** (ROE, ROA, "
        "net/gross/operating margins), **liquidity & leverage** (current ratio, "
        "quick ratio, debt/equity, debt/assets), **cash flow** (FCF yield, "
        "dividend yield), and **growth** (revenue & earnings YoY). "
        "Use `?years=N` (default 5, max 20) to set the historical look-back window. "
        "Add `?summarize=true` to receive only the `ratios_summary` object without "
        "the per-year table."
    ),
)
def fundamentals(
    ticker: str,
    years: int = Query(default=5, ge=1, le=20, description="Number of historical fiscal years"),
    summarize: bool = Query(
        default=False,
        description="Return only ratios_summary (no per_year table)",
    ),
    data_source: FinancialDataSource = Depends(get_data_source),
) -> FundamentalsResponse:
    """Return financial fundamentals for a given ticker."""
    ticker = ticker.upper()

    try:
        result = Fundamentals(data_source).compute(ticker, years=years)
    except (ValueError, LookupError) as exc:
        logger.warning("404 fundamentals %s: %s", ticker, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Error computing fundamentals for %s: %s", ticker, exc, exc_info=True)
        raise HTTPException(
            status_code=404,
            detail=f"No data found for ticker '{ticker}': {exc}",
        ) from exc

    if result.per_year.empty:
        logger.warning("No annual filings found for ticker '%s'", ticker)
        raise HTTPException(
            status_code=404,
            detail=f"No annual filings found for ticker '{ticker}'",
        )

    # ── Ratios summary ─────────────────────────────────────────────────
    summary = FundamentalsRatiosSummary(
        avg_pe=_f(result.avg_pe),
        avg_pb=_f(result.avg_pb),
        avg_ps=_f(result.avg_ps),
        avg_roe=_f(result.avg_roe),
        avg_roa=_f(result.avg_roa),
        avg_npm=_f(result.avg_npm),
        avg_gross_margin=_f(result.avg_gross_margin),
        avg_operating_margin=_f(result.avg_operating_margin),
        avg_current_ratio=_f(result.avg_current_ratio),
        avg_quick_ratio=_f(result.avg_quick_ratio),
        avg_debt_to_equity=_f(result.avg_debt_to_equity),
        avg_debt_to_assets=_f(result.avg_debt_to_assets),
        avg_fcf_yield=_f(result.avg_fcf_yield),
        avg_dividend_yield=_f(result.avg_dividend_yield),
        avg_revenue_growth=_f(result.avg_revenue_growth),
        avg_earnings_growth=_f(result.avg_earnings_growth),
    )

    if summarize:
        return FundamentalsResponse(ticker=result.ticker, ratios_summary=summary)

    # ── Market snapshot ────────────────────────────────────────────────
    last_div = result.last_dividend or {}
    snapshot = FundamentalsMarketSnapshot(
        current_price=_f(result.current_price),
        market_cap=_f(result.market_cap),
        shares_outstanding=_f(result.shares_outstanding) if result.shares_outstanding else None,
        avg_volume=_f(result.avg_volume) if result.avg_volume else None,
        last_dividend_date=last_div.get("date"),
        last_dividend_amount=_f(float(last_div["amount"])) if "amount" in last_div else None,
        beta=_f(result.beta) if result.beta is not None else None,
        week_52_high=_f(result.week_52_high) if result.week_52_high is not None else None,
        week_52_low=_f(result.week_52_low) if result.week_52_low is not None else None,
    )

    # ── Per-year rows ──────────────────────────────────────────────────
    per_year_list = []
    for yr, row in result.per_year.iterrows():
        per_year_list.append(FundamentalsPerYearRow(
            year=int(yr),
            avg_price=_f(row.get("avg_price")),
            revenue=_f(row.get("revenue")),
            gross_profit=_f(row.get("gross_profit")),
            operating_income=_f(row.get("operating_income")),
            net_income=_f(row.get("net_income")),
            eps=_f(row.get("eps")),
            total_assets=_f(row.get("total_assets")),
            total_liabilities=_f(row.get("total_liabilities")),
            equity=_f(row.get("equity")),
            current_assets=_f(row.get("current_assets")),
            current_liabilities=_f(row.get("current_liabilities")),
            operating_cashflow=_f(row.get("operating_cashflow")),
            capex=_f(row.get("capex")),
            free_cashflow=_f(row.get("free_cashflow")),
            book_value_per_share=_f(row.get("book_value_per_share")),
            revenue_per_share=_f(row.get("revenue_per_share")),
            fcf_per_share=_f(row.get("fcf_per_share")),
            dividend_per_share=_f(row.get("dividend_per_share")),
            pe=_f(row.get("pe")),
            pb=_f(row.get("pb")),
            ps=_f(row.get("ps")),
            roe=_f(row.get("roe")),
            roa=_f(row.get("roa")),
            npm=_f(row.get("npm")),
            gross_margin=_f(row.get("gross_margin")),
            operating_margin=_f(row.get("operating_margin")),
            current_ratio=_f(row.get("current_ratio")),
            quick_ratio=_f(row.get("quick_ratio")),
            debt_to_equity=_f(row.get("debt_to_equity")),
            debt_to_assets=_f(row.get("debt_to_assets")),
            fcf_yield=_f(row.get("fcf_yield")),
            dividend_yield=_f(row.get("dividend_yield")),
            revenue_growth=_f(row.get("revenue_growth")),
            earnings_growth=_f(row.get("earnings_growth")),
        ))

    return FundamentalsResponse(
        ticker=result.ticker,
        market_snapshot=snapshot,
        ratios_summary=summary,
        per_year=per_year_list,
    )
