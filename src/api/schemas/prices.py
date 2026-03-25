"""
Pydantic schemas for the price history endpoint.
"""
from __future__ import annotations


from pydantic import BaseModel


class PriceBar(BaseModel):
    """One daily OHLCV bar."""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class PricesResponse(BaseModel):
    """Full prices response for a ticker."""
    ticker: str
    name: str | None = None
    exchange: str | None = None
    period: str
    bars: list[PriceBar]
