"""
Pydantic response schemas for the GMR Short-term endpoint.
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class GMRShortRatioSchema(BaseModel):
    """The core GMR short-term verdict + metrics."""
    passes: bool
    flags: dict[str, bool]
    win_probability: Optional[float] = None
    avg_v_up: Optional[float] = None
    avg_v_down: Optional[float] = None
    mat_43d: Optional[float] = None
    diff_mat_pct: Optional[float] = None


class MarketSnapshotShortSchema(BaseModel):
    """Current market snapshot for the short-term endpoint."""
    current_price: Optional[float] = None
    avg_volume: Optional[float] = None


class MonthlyBreakdownSchema(BaseModel):
    """VUp / VDown for a single calendar month."""
    month: str                       # e.g. "2024-07"
    v_up: Optional[float] = None
    v_down: Optional[float] = None


class GMRShortResponse(BaseModel):
    """
    Full response from GET /{ticker}/gmr_short.

    When ?summarize=true  → market_snapshot and monthly_breakdown are omitted.
    When ?summarize=false → all fields are present.
    """
    ticker: str
    gmr_ratio: GMRShortRatioSchema
    market_snapshot: Optional[MarketSnapshotShortSchema] = None
    monthly_breakdown: Optional[list[MonthlyBreakdownSchema]] = None
