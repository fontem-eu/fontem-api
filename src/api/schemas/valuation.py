"""
Pydantic response schemas for the /valuation endpoint.
"""
from __future__ import annotations


from pydantic import BaseModel


class ValuationPerYearRow(BaseModel):
    """Per-fiscal-year raw inputs and computed enterprise valuation metrics."""
    year: int
    # Raw inputs (for transparency / cross-checking)
    da: float | None = None
    interest_expense: float | None = None
    cash_and_equivalents: float | None = None
    long_term_debt: float | None = None
    # EBITDA
    ebitda: float | None = None
    ebitda_margin: float | None = None           # %
    # Leverage
    net_debt: float | None = None
    net_debt_to_ebitda: float | None = None
    # Debt serviceability
    interest_coverage: float | None = None
    # Capital efficiency
    effective_tax_rate: float | None = None      # %
    nopat: float | None = None
    invested_capital: float | None = None
    roic: float | None = None                    # %


class ValuationSummary(BaseModel):
    """Averages of valuation metrics over the look-back window."""
    avg_ebitda_margin: float | None = None       # %
    avg_roic: float | None = None                # %
    avg_interest_coverage: float | None = None
    avg_net_debt_to_ebitda: float | None = None


class ValuationSnapshot(BaseModel):
    """Current enterprise-value metrics derived from live market data + latest EDGAR year."""
    enterprise_value: float | None = None
    market_cap: float | None = None
    ev_ebitda: float | None = None
    ev_revenue: float | None = None
    ev_fcf: float | None = None
    ev_ebit: float | None = None


class ValuationResponse(BaseModel):
    """Root response for the /valuation endpoint."""
    ticker: str
    valuation_snapshot: ValuationSnapshot | None = None
    summary: ValuationSummary
    per_year: list[ValuationPerYearRow | None] = None
