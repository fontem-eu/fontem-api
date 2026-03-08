"""
Pydantic response schemas for the GMR Long-term endpoint.
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class GMRLongRatioSchema(BaseModel):
    """The core GMR verdict + averaged ratios."""
    passes: bool
    flags: dict[str, bool]
    avg_pe: Optional[float] = None
    avg_pb: Optional[float] = None
    avg_roe: Optional[float] = None
    avg_npm: Optional[float] = None
    avg_debt_equity: Optional[float] = None
    avg_dividend_yield: Optional[float] = None
    avg_quick_ratio: Optional[float] = None
    avg_fcf: Optional[float] = None


class LastDividendSchema(BaseModel):
    date: Optional[str] = None
    amount: Optional[float] = None


class MarketSnapshotLongSchema(BaseModel):
    current_price: Optional[float] = None
    avg_volume: Optional[float] = None
    last_dividend: Optional[LastDividendSchema] = None


class PerYearRatiosSchema(BaseModel):
    """All raw data + computed ratios for a single fiscal year."""
    year: int
    avg_price: Optional[float] = None
    revenue: Optional[float] = None
    net_income: Optional[float] = None
    equity: Optional[float] = None
    total_liabilities: Optional[float] = None
    shares: Optional[float] = None
    dividends: Optional[float] = None
    # Computed ratios
    pe: Optional[float] = None
    pb: Optional[float] = None
    roe: Optional[float] = None
    npm: Optional[float] = None
    debt_equity: Optional[float] = None
    dividend_yield: Optional[float] = None
    quick_ratio: Optional[float] = None
    free_cashflow: Optional[float] = None


class GMRLongResponse(BaseModel):
    """
    Full response from GET /{ticker}/gmr_long.

    When ?summarize=true  → market_snapshot and per_year are omitted.
    When ?summarize=false → all fields are present.
    """
    ticker: str
    gmr_ratio: GMRLongRatioSchema
    market_snapshot: Optional[MarketSnapshotLongSchema] = None
    per_year: Optional[list[PerYearRatiosSchema]] = None
