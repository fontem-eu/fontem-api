"""
Pydantic response schemas for the /fundamentals endpoint.
"""
from __future__ import annotations


from pydantic import BaseModel


class FundamentalsMarketSnapshot(BaseModel):
    """Current market data: price, market cap, volume, last dividend, and price range."""
    current_price: float | None = None
    market_cap: float | None = None
    shares_outstanding: float | None = None
    avg_volume: float | None = None
    last_dividend_date: str | None = None
    last_dividend_amount: float | None = None
    beta: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None


class FundamentalsRatiosSummary(BaseModel):
    """Averages of all key ratios over the look-back window."""
    # Valuation
    avg_pe: float | None = None
    avg_pb: float | None = None
    avg_ps: float | None = None
    # Profitability
    avg_roe: float | None = None
    avg_roa: float | None = None
    avg_npm: float | None = None
    avg_gross_margin: float | None = None
    avg_operating_margin: float | None = None
    # Liquidity / Leverage
    avg_current_ratio: float | None = None
    avg_quick_ratio: float | None = None
    avg_debt_to_equity: float | None = None
    avg_debt_to_assets: float | None = None
    # Cash flow
    avg_fcf_yield: float | None = None
    avg_dividend_yield: float | None = None
    # Growth
    avg_revenue_growth: float | None = None
    avg_earnings_growth: float | None = None


class FundamentalsPerYearRow(BaseModel):
    """All raw figures and computed ratios for a single fiscal year."""
    year: int
    avg_price: float | None = None
    # Income statement
    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    eps: float | None = None
    # Balance sheet
    total_assets: float | None = None
    total_liabilities: float | None = None
    equity: float | None = None
    current_assets: float | None = None
    current_liabilities: float | None = None
    # Cash flow
    operating_cashflow: float | None = None
    capex: float | None = None
    free_cashflow: float | None = None
    # Per-share
    book_value_per_share: float | None = None
    revenue_per_share: float | None = None
    fcf_per_share: float | None = None
    dividend_per_share: float | None = None
    # Valuation ratios
    pe: float | None = None
    pb: float | None = None
    ps: float | None = None
    # Profitability ratios
    roe: float | None = None
    roa: float | None = None
    npm: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    # Liquidity / Leverage ratios
    current_ratio: float | None = None
    quick_ratio: float | None = None
    debt_to_equity: float | None = None
    debt_to_assets: float | None = None
    # Cash flow ratios
    fcf_yield: float | None = None
    dividend_yield: float | None = None
    # Growth
    revenue_growth: float | None = None
    earnings_growth: float | None = None


class FundamentalsResponse(BaseModel):
    """Root response for the /fundamentals endpoint."""
    ticker: str
    gmr_id: str | None = None
    company_name: str | None = None
    data_source: str | None = None
    market_snapshot: FundamentalsMarketSnapshot | None = None
    ratios_summary: FundamentalsRatiosSummary
    per_year: list[FundamentalsPerYearRow | None] = None
