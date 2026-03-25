"""
Ticker API Schemas
==================
Pydantic models for ticker list and search responses.
"""
from __future__ import annotations


from pydantic import BaseModel, ConfigDict, Field


class TickerInfo(BaseModel):
    """
    Comprehensive ticker information with rich metadata for UI display.
    """
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "cik": "0000320193",
                "sic": "3571",
                "sic_description": "Electronic Computers",
                "exchange": "NASDAQ",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "country": "US",
                "currency": "USD",
                "is_active": True,
                "last_updated": "2023-01-15T10:30:00Z",
                "search_name": "apple inc. aapl",
                "search_keywords": "apple computer technology consumer electronics nasdaq",
            }
        },
    )

    symbol: str = Field(..., description="Stock ticker symbol")
    name: str = Field(..., description="Company name")
    cik: str = Field(..., description="SEC Central Index Key")
    sic: str | None = Field(None, description="Standard Industrial Classification code")
    sic_description: str | None = Field(None, description="SIC code description")
    exchange: str | None = Field(None, description="Stock exchange")
    sector: str | None = Field(None, description="Business sector")
    industry: str | None = Field(None, description="Specific industry")
    country: str | None = Field(None, description="Country of incorporation")
    currency: str | None = Field(None, description="Currency for financials")
    is_active: bool | None = Field(None, description="Whether company is actively trading")
    last_updated: str | None = Field(None, description="Last update timestamp")

    # Search-friendly fields (not displayed but used for filtering)
    search_name: str | None = Field(None, description="Combined name and symbol for search")
    search_keywords: str | None = Field(None, description="Additional search keywords")


class TickerSearchResponse(BaseModel):
    """
    Response model for ticker search results.
    """
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "query": "apple",
                "results": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "cik": "0000320193",
                        "exchange": "NASDAQ",
                        "country": "US",
                        "currency": "USD",
                        "is_active": True,
                    }
                ],
                "count": 1,
                "total_available": 10000,
            }
        },
    )

    query: str = Field(..., description="Search query that was executed")
    results: list[TickerInfo] = Field(..., description="Matching ticker results")
    count: int = Field(..., description="Number of results returned")
    total_available: int = Field(..., description="Total number of tickers available")
