"""
Pydantic response schemas for the GMR Long-term endpoint.
"""
from __future__ import annotations

from pydantic import BaseModel


class GMRLongRatioSchema(BaseModel):
    """The core GMR verdict + averaged ratios."""
    passes: bool
    flags: dict[str, bool]
    avg_pe: float | None = None
    avg_pb: float | None = None
    avg_roe: float | None = None
    avg_npm: float | None = None
    avg_debt_equity: float | None = None
    avg_dividend_yield: float | None = None
    avg_quick_ratio: float | None = None
    avg_fcf: float | None = None


class LastDividendSchema(BaseModel):
    """Last dividend date and amount."""
    date: str | None = None
    amount: float | None = None


class MarketSnapshotLongSchema(BaseModel):
    """Current market snapshot for the long-term endpoint."""
    current_price: float | None = None
    avg_volume: float | None = None
    last_dividend: LastDividendSchema | None = None


class PerYearRatiosSchema(BaseModel):
    """All raw data + computed ratios for a single fiscal year."""
    year: int
    avg_price: float | None = None
    revenue: float | None = None
    net_income: float | None = None
    equity: float | None = None
    total_liabilities: float | None = None
    shares: float | None = None
    dividends: float | None = None
    # Computed ratios
    pe: float | None = None
    pb: float | None = None
    roe: float | None = None
    npm: float | None = None
    debt_equity: float | None = None
    dividend_yield: float | None = None
    quick_ratio: float | None = None
    free_cashflow: float | None = None


class GMRLongResponse(BaseModel):
    """
    Full response from GET /{ticker}/gmr_long.

    When ?summarize=true  → market_snapshot and per_year are omitted.
    When ?summarize=false → all fields are present.
    """
    ticker: str
    gmr_ratio: GMRLongRatioSchema
    market_snapshot: MarketSnapshotLongSchema | None = None
    per_year: list[PerYearRatiosSchema | None] = None
