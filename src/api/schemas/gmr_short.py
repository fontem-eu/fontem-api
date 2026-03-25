"""
Pydantic response schemas for the GMR Short-term endpoint.
"""
from __future__ import annotations

from pydantic import BaseModel


class GMRShortRatioSchema(BaseModel):
    """The core GMR short-term verdict + metrics."""
    passes: bool
    flags: dict[str, bool]
    win_probability: float | None = None
    avg_v_up: float | None = None
    avg_v_down: float | None = None
    mat_43d: float | None = None
    diff_mat_pct: float | None = None


class MarketSnapshotShortSchema(BaseModel):
    """Current market snapshot for the short-term endpoint."""
    current_price: float | None = None
    avg_volume: float | None = None


class MonthlyBreakdownSchema(BaseModel):
    """VUp / VDown for a single calendar month."""
    month: str                       # e.g. "2024-07"
    v_up: float | None = None
    v_down: float | None = None


class GMRShortResponse(BaseModel):
    """
    Full response from GET /{ticker}/gmr_short.

    When ?summarize=true  → market_snapshot and monthly_breakdown are omitted.
    When ?summarize=false → all fields are present.
    """
    ticker: str
    gmr_ratio: GMRShortRatioSchema
    market_snapshot: MarketSnapshotShortSchema | None = None
    monthly_breakdown: list[MonthlyBreakdownSchema | None] = None
