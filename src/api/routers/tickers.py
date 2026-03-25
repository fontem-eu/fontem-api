"""
Ticker API Endpoints
=====================
GET /tickers/search   — search tickers by name, symbol, or keywords
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from src.api.dependencies import get_data_source
from src.api.schemas.tickers import TickerSearchResponse
from src.analysis.gmr_data_source import FinancialDataSource

router = APIRouter(
    prefix="/tickers",
    tags=["Ticker Discovery"],
)


@router.get(
    "/search",
    response_model=TickerSearchResponse,
    response_model_exclude_none=True,
    summary="Search Tickers",
    description=(
        "Search companies by name, ticker symbol, or keywords. "
        "Returns matching tickers with full metadata. "
        "Useful for autocomplete search boxes."
    ),
)
def search_tickers(
    query: str = Query(
        ...,
        description="Search term (company name, ticker symbol, or keywords)",
        min_length=1,
    ),
    limit: int = Query(
        10,
        description="Maximum number of results",
        ge=1,
        le=50,
    ),
    data_source: FinancialDataSource = Depends(get_data_source),
) -> TickerSearchResponse:
    """Search tickers by name, symbol, or keywords."""
    results = data_source.search_tickers(query, limit)
    return TickerSearchResponse(
        query=query,
        results=results,
        count=len(results),
        total_available=len(data_source.get_available_tickers()),
    )
