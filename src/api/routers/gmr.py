"""
GMR API Endpoints
==================
GET /{ticker}/gmr_long   — long-term value-investing screen
GET /{ticker}/gmr_short  — short-term swing-trading screen

Both endpoints accept an optional ?summarize=true query parameter.
When summarize=true only the gmr_ratio object is returned (no per-year
table or monthly breakdown), which is useful for dashboards / alerts.

A 404 is returned when:
  • the ticker has no filings on EDGAR (GMR Long), or
  • the ticker is unknown / has no price history (GMR Short), or
  • the underlying data source raises a ValueError / LookupError.
"""
from __future__ import annotations

import math
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.analysis.gmr_data_source import GMRDataSource
from src.analysis.gmr_long import GMRLong
from src.analysis.gmr_short import GMRShort
from src.api.dependencies import get_data_source
from src.api.schemas.gmr_long import (
    GMRLongResponse,
    GMRLongRatioSchema,
    LastDividendSchema,
    MarketSnapshotLongSchema,
    PerYearRatiosSchema,
)
from src.api.schemas.gmr_short import (
    GMRShortResponse,
    GMRShortRatioSchema,
    MarketSnapshotShortSchema,
    MonthlyBreakdownSchema,
)

router = APIRouter(tags=["GMR Analysis"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _f(value: float) -> Optional[float]:
    """Convert NaN / Inf to None for JSON serialisation."""
    if value is None:
        return None
    try:
        return None if (math.isnan(value) or math.isinf(value)) else value
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# GMR Long
# ---------------------------------------------------------------------------

@router.get(
    "/{ticker}/gmr_long",
    response_model=GMRLongResponse,
    response_model_exclude_none=True,
    summary="GMR Long-Term Value Screen",
    description=(
        "Runs the GMR long-term fundamental screen for a given ticker. "
        "Returns per-year ratios (P/E, P/B, ROE, NPM, D/E, Div Yield, Quick Ratio, FCF) "
        "averaged over the look-back window, plus a pass/fail verdict per ratio. "
        "Add **?summarize=true** to receive only the `gmr_ratio` object."
    ),
)
def gmr_long(
    ticker: str,
    summarize: bool = Query(default=False, description="Return only the gmr_ratio object"),
    data_source: GMRDataSource = Depends(get_data_source),
) -> GMRLongResponse:
    ticker = ticker.upper()

    try:
        result = GMRLong(data_source).compute(ticker)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"No data found for ticker '{ticker}': {exc}",
        )

    if result.per_year.empty:
        raise HTTPException(
            status_code=404,
            detail=f"No annual filings found for ticker '{ticker}'",
        )

    ratio = GMRLongRatioSchema(
        passes=result.passes_all,
        flags=result.flags,
        avg_pe=_f(result.avg_pe),
        avg_pb=_f(result.avg_pb),
        avg_roe=_f(result.avg_roe),
        avg_npm=_f(result.avg_npm),
        avg_debt_equity=_f(result.avg_debt_equity),
        avg_dividend_yield=_f(result.avg_dividend_yield),
        avg_quick_ratio=_f(result.avg_quick_ratio),
        avg_fcf=_f(result.avg_fcf),
    )

    if summarize:
        return GMRLongResponse(ticker=result.ticker, gmr_ratio=ratio)

    # ── Build market snapshot ──────────────────────────────────────────
    last_div_raw = result.last_dividend or {}
    last_div = LastDividendSchema(
        date=last_div_raw.get("date"),
        amount=_f(float(last_div_raw["amount"])) if "amount" in last_div_raw else None,
    )
    snapshot = MarketSnapshotLongSchema(
        current_price=_f(result.current_price),
        avg_volume=_f(result.avg_volume) if result.avg_volume else None,
        last_dividend=last_div,
    )

    # ── Build per-year list ────────────────────────────────────────────
    per_year_list = []
    for yr, row in result.per_year.iterrows():
        per_year_list.append(PerYearRatiosSchema(
            year=int(yr),
            avg_price=_f(row.get("avg_price")),
            revenue=_f(row.get("revenue")),
            net_income=_f(row.get("net_income")),
            equity=_f(row.get("equity")),
            total_liabilities=_f(row.get("total_liabilities")),
            shares=_f(row.get("shares")),
            dividends=_f(row.get("dividends")),
            pe=_f(row.get("pe")),
            pb=_f(row.get("pb")),
            roe=_f(row.get("roe")),
            npm=_f(row.get("npm")),
            debt_equity=_f(row.get("debt_equity")),
            dividend_yield=_f(row.get("dividend_yield")),
            quick_ratio=_f(row.get("quick_ratio")),
            free_cashflow=_f(row.get("free_cashflow")),
        ))

    return GMRLongResponse(
        ticker=result.ticker,
        gmr_ratio=ratio,
        market_snapshot=snapshot,
        per_year=per_year_list,
    )


# ---------------------------------------------------------------------------
# GMR Short
# ---------------------------------------------------------------------------

@router.get(
    "/{ticker}/gmr_short",
    response_model=GMRShortResponse,
    response_model_exclude_none=True,
    summary="GMR Short-Term Swing Screen",
    description=(
        "Runs the GMR short-term swing-trading screen for a given ticker. "
        "Returns win probability, average VUp/VDown, 43-day MAT, diffMAT, "
        "and a per-month volatility breakdown. "
        "Add **?summarize=true** to receive only the `gmr_ratio` object."
    ),
)
def gmr_short(
    ticker: str,
    summarize: bool = Query(default=False, description="Return only the gmr_ratio object"),
    data_source: GMRDataSource = Depends(get_data_source),
) -> GMRShortResponse:
    ticker = ticker.upper()

    try:
        result = GMRShort(data_source).compute(ticker)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"No data found for ticker '{ticker}': {exc}",
        )

    # If the data source returns an empty result (unknown ticker / no history)
    if math.isnan(result.current_price) and result.monthly_breakdown.empty:
        raise HTTPException(
            status_code=404,
            detail=f"No price history found for ticker '{ticker}'",
        )

    ratio = GMRShortRatioSchema(
        passes=result.passes_all,
        flags=result.flags,
        win_probability=_f(result.win_probability),
        avg_v_up=_f(result.avg_v_up),
        avg_v_down=_f(result.avg_v_down),
        mat_43d=_f(result.mat_43d),
        diff_mat_pct=_f(result.diff_mat_pct),
    )

    if summarize:
        return GMRShortResponse(ticker=result.ticker, gmr_ratio=ratio)

    snapshot = MarketSnapshotShortSchema(
        current_price=_f(result.current_price),
        avg_volume=_f(result.avg_volume) if result.avg_volume else None,
    )

    monthly = []
    for period, row in result.monthly_breakdown.iterrows():
        monthly.append(MonthlyBreakdownSchema(
            month=str(period),
            v_up=_f(row.get("v_up")),
            v_down=_f(row.get("v_down")),
        ))

    return GMRShortResponse(
        ticker=result.ticker,
        gmr_ratio=ratio,
        market_snapshot=snapshot,
        monthly_breakdown=monthly,
    )
