"""
Pydantic response schemas for the /valuation endpoint.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class ValuationPerYearRow(BaseModel):
    """Per-fiscal-year raw inputs and computed enterprise valuation metrics."""
    year: int
    # Raw inputs (for transparency / cross-checking)
    da: Optional[float] = None
    interest_expense: Optional[float] = None
    cash_and_equivalents: Optional[float] = None
    long_term_debt: Optional[float] = None
    # EBITDA
    ebitda: Optional[float] = None
    ebitda_margin: Optional[float] = None           # %
    # Leverage
    net_debt: Optional[float] = None
    net_debt_to_ebitda: Optional[float] = None
    # Debt serviceability
    interest_coverage: Optional[float] = None
    # Capital efficiency
    effective_tax_rate: Optional[float] = None      # %
    nopat: Optional[float] = None
    invested_capital: Optional[float] = None
    roic: Optional[float] = None                    # %


class ValuationSummary(BaseModel):
    """Averages of valuation metrics over the look-back window."""
    avg_ebitda_margin: Optional[float] = None       # %
    avg_roic: Optional[float] = None                # %
    avg_interest_coverage: Optional[float] = None
    avg_net_debt_to_ebitda: Optional[float] = None


class ValuationSnapshot(BaseModel):
    """Current enterprise-value metrics derived from live market data + latest EDGAR year."""
    enterprise_value: Optional[float] = None
    market_cap: Optional[float] = None
    ev_ebitda: Optional[float] = None
    ev_revenue: Optional[float] = None
    ev_fcf: Optional[float] = None
    ev_ebit: Optional[float] = None


class ValuationResponse(BaseModel):
    """Root response for the /valuation endpoint."""
    ticker: str
    valuation_snapshot: Optional[ValuationSnapshot] = None
    summary: ValuationSummary
    per_year: Optional[List[ValuationPerYearRow]] = None
