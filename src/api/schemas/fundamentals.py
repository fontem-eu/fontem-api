"""
Pydantic response schemas for the /fundamentals endpoint.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class FundamentalsMarketSnapshot(BaseModel):
    """Current market data: price, market cap, volume, last dividend, and price range."""
    current_price: Optional[float] = None
    market_cap: Optional[float] = None
    shares_outstanding: Optional[float] = None
    avg_volume: Optional[float] = None
    last_dividend_date: Optional[str] = None
    last_dividend_amount: Optional[float] = None
    beta: Optional[float] = None
    week_52_high: Optional[float] = None
    week_52_low: Optional[float] = None


class FundamentalsRatiosSummary(BaseModel):
    """Averages of all key ratios over the look-back window."""
    # Valuation
    avg_pe: Optional[float] = None
    avg_pb: Optional[float] = None
    avg_ps: Optional[float] = None
    # Profitability
    avg_roe: Optional[float] = None
    avg_roa: Optional[float] = None
    avg_npm: Optional[float] = None
    avg_gross_margin: Optional[float] = None
    avg_operating_margin: Optional[float] = None
    # Liquidity / Leverage
    avg_current_ratio: Optional[float] = None
    avg_quick_ratio: Optional[float] = None
    avg_debt_to_equity: Optional[float] = None
    avg_debt_to_assets: Optional[float] = None
    # Cash flow
    avg_fcf_yield: Optional[float] = None
    avg_dividend_yield: Optional[float] = None
    # Growth
    avg_revenue_growth: Optional[float] = None
    avg_earnings_growth: Optional[float] = None


class FundamentalsPerYearRow(BaseModel):
    """All raw figures and computed ratios for a single fiscal year."""
    year: int
    avg_price: Optional[float] = None
    # Income statement
    revenue: Optional[float] = None
    gross_profit: Optional[float] = None
    operating_income: Optional[float] = None
    net_income: Optional[float] = None
    eps: Optional[float] = None
    # Balance sheet
    total_assets: Optional[float] = None
    total_liabilities: Optional[float] = None
    equity: Optional[float] = None
    current_assets: Optional[float] = None
    current_liabilities: Optional[float] = None
    # Cash flow
    operating_cashflow: Optional[float] = None
    capex: Optional[float] = None
    free_cashflow: Optional[float] = None
    # Per-share
    book_value_per_share: Optional[float] = None
    revenue_per_share: Optional[float] = None
    fcf_per_share: Optional[float] = None
    dividend_per_share: Optional[float] = None
    # Valuation ratios
    pe: Optional[float] = None
    pb: Optional[float] = None
    ps: Optional[float] = None
    # Profitability ratios
    roe: Optional[float] = None
    roa: Optional[float] = None
    npm: Optional[float] = None
    gross_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    # Liquidity / Leverage ratios
    current_ratio: Optional[float] = None
    quick_ratio: Optional[float] = None
    debt_to_equity: Optional[float] = None
    debt_to_assets: Optional[float] = None
    # Cash flow ratios
    fcf_yield: Optional[float] = None
    dividend_yield: Optional[float] = None
    # Growth
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None


class FundamentalsResponse(BaseModel):
    """Root response for the /fundamentals endpoint."""
    ticker: str
    market_snapshot: Optional[FundamentalsMarketSnapshot] = None
    ratios_summary: FundamentalsRatiosSummary
    per_year: Optional[List[FundamentalsPerYearRow]] = None
