"""
Pydantic schemas for the GET /{ticker}/gmr_data endpoint.

The response mirrors every cell in the GMR spreadsheet so the caller
has all the raw numbers needed to feed the model without further
data wrangling.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class CurrentSnapshotSchema(BaseModel):
    """Live/most-recent values shown in the spreadsheet header row."""
    price: Optional[float] = None
    avg_volume: Optional[float] = None
    # Most-recent quarterly balance sheet
    current_assets: Optional[float] = None
    inventory: Optional[float] = None
    prepaid_expenses: Optional[float] = None
    current_liabilities: Optional[float] = None
    total_debt: Optional[float] = None
    equity: Optional[float] = None
    shares: Optional[float] = None
    # Last dividend
    last_dividend_date: Optional[str] = None
    last_dividend_amount: Optional[float] = None
    # Last stock split
    last_split_year: Optional[int] = None
    last_split_ratio: Optional[float] = None


class AnnualRowSchema(BaseModel):
    """One row in the per-year spreadsheet table."""
    year: int
    avg_price: Optional[float] = None
    revenue: Optional[float] = None
    earnings: Optional[float] = None        # net income
    total_assets: Optional[float] = None
    liabilities: Optional[float] = None     # total liabilities
    equity: Optional[float] = None
    shares: Optional[float] = None          # shares outstanding
    dividend: Optional[float] = None        # total annual dividend per share
    current_assets: Optional[float] = None
    inventory: Optional[float] = None
    prepaid_expenses: Optional[float] = None
    current_liabilities: Optional[float] = None
    cfo: Optional[float] = None             # operating cash flow
    delta_ppe: Optional[float] = None       # capital expenditure (negative = outflow)
    splits: Optional[float] = None          # split ratio that year, 0 if none


class GMRDataResponse(BaseModel):
    """Full response for GET /{ticker}/gmr_data."""
    ticker: str
    current_snapshot: CurrentSnapshotSchema
    annual_data: List[AnnualRowSchema]
