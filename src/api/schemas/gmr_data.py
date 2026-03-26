"""
Pydantic schemas for the GET /{ticker}/gmr_data endpoint.

The response mirrors every cell in the GMR spreadsheet so the caller
has all the raw numbers needed to feed the model without further
data wrangling.
"""
from __future__ import annotations


from pydantic import BaseModel


class CurrentSnapshotSchema(BaseModel):
    """Live/most-recent values shown in the spreadsheet header row."""
    price: float | None = None
    avg_volume: float | None = None
    # Most-recent quarterly balance sheet
    current_assets: float | None = None
    inventory: float | None = None
    prepaid_expenses: float | None = None
    current_liabilities: float | None = None
    total_debt: float | None = None
    equity: float | None = None
    shares: float | None = None
    # Last dividend
    last_dividend_date: str | None = None
    last_dividend_amount: float | None = None
    # Last stock split
    last_split_year: int | None = None
    last_split_ratio: float | None = None


class AnnualRowSchema(BaseModel):
    """One row in the per-year spreadsheet table."""
    year: int
    avg_price: float | None = None
    revenue: float | None = None
    earnings: float | None = None        # net income
    total_assets: float | None = None
    liabilities: float | None = None     # total liabilities
    equity: float | None = None
    shares: float | None = None          # shares outstanding
    dividend: float | None = None        # total annual dividend per share
    current_assets: float | None = None
    inventory: float | None = None
    prepaid_expenses: float | None = None
    current_liabilities: float | None = None
    cfo: float | None = None             # operating cash flow
    delta_ppe: float | None = None       # capital expenditure (negative = outflow)
    splits: float | None = None          # split ratio that year, 0 if none


class GMRDataResponse(BaseModel):
    """Full response for GET /{ticker}/gmr_data."""
    ticker: str
    data_source: str | None = None
    current_snapshot: CurrentSnapshotSchema
    annual_data: list[AnnualRowSchema]
