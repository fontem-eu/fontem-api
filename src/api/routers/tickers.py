"""
Ticker API Endpoints
=====================
GET /tickers          — list all available tickers with metadata
GET /tickers/search   — search tickers by name, symbol, or keywords

Provides comprehensive company information for UI components like:
- Autocomplete search boxes
- Ticker selection tables
- Filterable company lists
- Sector/industry breakdowns
"""
from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, Depends, Query

from src.data.live_data_source import LiveDataSource
from src.api.dependencies import get_data_source
from src.api.schemas.tickers import TickerInfo, TickerSearchResponse

router = APIRouter(
    prefix="/tickers",
    tags=["Ticker Discovery"],
    responses={404: {"description": "No tickers found"}},
)

@router.get(
    "/",
    response_model=List[TickerInfo],
    response_model_exclude_none=True,
    summary="List All Available Tickers",
    description=(
        "Returns a comprehensive list of all companies that file with SEC EDGAR. "
        "Each ticker includes rich metadata for UI display and filtering: "
        "company name, CIK, SIC codes, exchange, sector, industry, etc. "
        "Results are cached for 24 hours (SEC updates daily)."
    ),
)
def list_tickers(
    limit: Optional[int] = Query(
        None,
        description="Maximum number of tickers to return",
        ge=1,
        le=10000
    ),
    offset: Optional[int] = Query(
        0,
        description="Pagination offset",
        ge=0
    ),
    data_source: LiveDataSource = Depends(get_data_source)
) -> List[TickerInfo]:
    """
    List all tickers available in EDGAR database.

    Args:
        limit: Maximum number of results (default: all)
        offset: Pagination offset (default: 0)

    Returns:
        List of ticker dictionaries with rich metadata
    """
    all_tickers = data_source.get_available_tickers()

    # Apply pagination
    if limit is not None:
        return all_tickers[offset:offset+limit]
    return all_tickers[offset:]

@router.get(
    "/search",
    response_model=TickerSearchResponse,
    response_model_exclude_none=True,
    summary="Search Tickers",
    description=(
        "Search companies by name, ticker symbol, or keywords. "
        "Returns matching tickers with full metadata. "
        "Useful for autocomplete search boxes and filtered views."
    ),
)
def search_tickers(
    query: str = Query(
        ...,
        description="Search term (company name, ticker symbol, or keywords)",
        min_length=1
    ),
    limit: int = Query(
        10,
        description="Maximum number of results",
        ge=1,
        le=50
    ),
    data_source: LiveDataSource = Depends(get_data_source)
) -> TickerSearchResponse:
    """
    Search tickers by name, symbol, or keywords.

    Args:
        query: Search term (case-insensitive)
        limit: Maximum number of results (default: 10)

    Returns:
        Search response with matching tickers
    """
    results = data_source.search_tickers(query, limit)

    return TickerSearchResponse(
        query=query,
        results=results,
        count=len(results),
        total_available=len(data_source.get_available_tickers())
    )

@router.get(
    "/sectors",
    response_model=List[str],
    summary="List Available Sectors",
    description=(
        "Returns list of unique sectors available in the ticker database. "
        "Useful for sector filter dropdowns in UI."
    ),
)
def list_sectors(
    data_source: LiveDataSource = Depends(get_data_source)
) -> List[str]:
    """
    Get list of unique sectors for filtering.

    Returns:
        List of unique sector names
    """
    all_tickers = data_source.get_available_tickers()
    sectors = {ticker['sector'] for ticker in all_tickers if ticker['sector']}
    return sorted(list(sectors))

@router.get(
    "/exchanges",
    response_model=List[str],
    summary="List Available Exchanges",
    description=(
        "Returns list of unique exchanges available in the ticker database. "
        "Useful for exchange filter dropdowns in UI."
    ),
)
def list_exchanges(
    data_source: LiveDataSource = Depends(get_data_source)
) -> List[str]:
    """
    Get list of unique exchanges for filtering.

    Returns:
        List of unique exchange names
    """
    all_tickers = data_source.get_available_tickers()
    exchanges = {ticker['exchange'] for ticker in all_tickers if ticker['exchange']}
    return sorted(list(exchanges))