"""
Enterprise Valuation Endpoint
==============================
GET /{ticker}/valuation

Returns enterprise-value-based and capital-efficiency metrics for a given
ticker, averaged over a configurable number of fiscal years.

Response includes:
  • valuation_snapshot — current EV multiples (EV/EBITDA, EV/Revenue, EV/FCF, EV/EBIT)
  • summary            — averages of EBITDA margin, ROIC, interest coverage, Net Debt/EBITDA
  • per_year           — per-fiscal-year raw inputs and computed metrics

Data sources:
  • EDGAR (10-K / 20-F / 40-F): operating income, D&A, interest expense, tax expense,
    equity, long-term debt, cash & equivalents, revenue, FCF
  • Yahoo Finance (live): current price, shares outstanding → market cap → EV

Use ?years=N (default 10, range 1–20) to control the historical window.
Use ?summarize=true to receive only ticker + summary (no per_year table).

A 404 is returned when:
  • the ticker has no filings on EDGAR, or
  • the ticker is unknown / data source raises ValueError / LookupError.
"""
# The try/except 404 pattern is intentionally identical across all analysis
# routers — it is standard FastAPI error handling, not a shared abstraction.
# pylint: disable=duplicate-code
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from src.analysis.gmr_data_source import FinancialDataSource
from src.analysis.valuation import Valuation
from dishka.integrations.fastapi import FromDishka, inject
from src.api.di import resolve_company_id
from src.data.graph.neo4j_client import Neo4jClient
from src.api.helpers import nan_to_none
from src.api.schemas.valuation import (
    ValuationPerYearRow,
    ValuationResponse,
    ValuationSnapshot,
    ValuationSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Valuation"])


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/{ticker}/valuation",
    response_model=ValuationResponse,
    response_model_exclude_none=True,
    summary="Enterprise Valuation",
    description=(
        "Returns enterprise-value-based and capital-efficiency metrics for a given ticker. "
        "Covers **EBITDA** and **EBITDA margin**, **Net Debt** and **Net Debt/EBITDA**, "
        "**Interest Coverage** (EBIT / Interest Expense), and **ROIC** "
        "(Return on Invested Capital). "
        "Live EV multiples (**EV/EBITDA**, **EV/Revenue**, **EV/FCF**, **EV/EBIT**) are "
        "computed from the current market cap plus the most recent year's net debt. "
        "Use `?years=N` (default 5, max 20) to set the historical look-back window. "
        "Add `?summarize=true` to receive only the `summary` object without the per-year table."
    ),
)
@inject
def valuation(
    ticker: str,
    years: int = Query(default=10, ge=1, le=20, description="Number of historical fiscal years"),
    summarize: bool = Query(
        default=False,
        description="Return only summary (no per_year table)",
    ),
    *,
    data_source: FromDishka[FinancialDataSource],
    neo4j: FromDishka[Neo4jClient],
) -> ValuationResponse:
    """Return enterprise valuation metrics for a given ticker."""
    ticker = ticker.upper()
    company_info = resolve_company_id(ticker, neo4j)

    try:
        result = Valuation(data_source).compute(ticker, years=years)
    except (ValueError, LookupError) as exc:
        logger.warning("404 valuation %s: %s", ticker, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Error computing valuation for %s: %s", ticker, exc, exc_info=True)
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

    # ── Summary ─────────────────────────────────────────────────────────
    summary = ValuationSummary(
        avg_ebitda_margin=nan_to_none(result.avg_ebitda_margin),
        avg_roic=nan_to_none(result.avg_roic),
        avg_interest_coverage=nan_to_none(result.avg_interest_coverage),
        avg_net_debt_to_ebitda=nan_to_none(result.avg_net_debt_to_ebitda),
    )

    if summarize:
        return ValuationResponse(
            ticker=result.ticker,
            gmr_id=company_info.get("gmr_id"),
            company_name=company_info.get("name"),
            data_source=data_source.get_data_source_name(ticker),
            summary=summary,
        )

    # ── Valuation snapshot ───────────────────────────────────────────────
    snap = ValuationSnapshot(
        enterprise_value=nan_to_none(result.enterprise_value),
        market_cap=nan_to_none(result.market_cap),
        ev_ebitda=nan_to_none(result.ev_ebitda),
        ev_revenue=nan_to_none(result.ev_revenue),
        ev_fcf=nan_to_none(result.ev_fcf),
        ev_ebit=nan_to_none(result.ev_ebit),
    )

    # ── Per-year rows ────────────────────────────────────────────────────
    per_year_list = []
    for yr, row in result.per_year.iterrows():
        per_year_list.append(ValuationPerYearRow(
            year=int(yr),
            da=nan_to_none(row.get("da")),
            interest_expense=nan_to_none(row.get("interest_expense")),
            cash_and_equivalents=nan_to_none(row.get("cash_and_equivalents")),
            long_term_debt=nan_to_none(row.get("long_term_debt")),
            ebitda=nan_to_none(row.get("ebitda")),
            ebitda_margin=nan_to_none(row.get("ebitda_margin")),
            net_debt=nan_to_none(row.get("net_debt")),
            net_debt_to_ebitda=nan_to_none(row.get("net_debt_to_ebitda")),
            interest_coverage=nan_to_none(row.get("interest_coverage")),
            effective_tax_rate=nan_to_none(row.get("effective_tax_rate")),
            nopat=nan_to_none(row.get("nopat")),
            invested_capital=nan_to_none(row.get("invested_capital")),
            roic=nan_to_none(row.get("roic")),
        ))

    return ValuationResponse(
        ticker=result.ticker,
        gmr_id=company_info.get("gmr_id"),
        company_name=company_info.get("name"),
        data_source=data_source.get_data_source_name(ticker),
        valuation_snapshot=snap,
        summary=summary,
        per_year=per_year_list,
    )
