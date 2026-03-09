"""
Ticker API Schemas
==================
Pydantic models for ticker list and search responses.
"""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field

class TickerInfo(BaseModel):
    """
    Comprehensive ticker information with rich metadata for UI display.
    """
    symbol: str = Field(..., description="Stock ticker symbol", example="AAPL")
    name: str = Field(..., description="Company name", example="Apple Inc.")
    cik: str = Field(..., description="SEC Central Index Key", example="0000320193")
    sic: Optional[str] = Field(None, description="Standard Industrial Classification code", example="3571")
    sic_description: Optional[str] = Field(None, description="SIC code description", example="Electronic Computers")
    exchange: Optional[str] = Field(None, description="Stock exchange", example="NASDAQ")
    sector: Optional[str] = Field(None, description="Business sector", example="Technology")
    industry: Optional[str] = Field(None, description="Specific industry", example="Consumer Electronics")
    country: Optional[str] = Field(None, description="Country of incorporation", example="US")
    currency: Optional[str] = Field(None, description="Currency for financials", example="USD")
    is_active: Optional[bool] = Field(None, description="Whether company is actively trading", example=True)
    last_updated: Optional[str] = Field(None, description="Last update timestamp", example="2023-01-15")

    # Search-friendly fields (not displayed but used for filtering)
    search_name: Optional[str] = Field(None, description="Combined name and symbol for search")
    search_keywords: Optional[str] = Field(None, description="Additional search keywords")

    class Config:
        from_attributes = True
        json_schema_extra = {
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
                "search_keywords": "apple computer technology consumer electronics nasdaq"
            }
        }

class TickerSearchResponse(BaseModel):
    """
    Response model for ticker search results.
    """
    query: str = Field(..., description="Search query that was executed", example="apple")
    results: List[TickerInfo] = Field(..., description="Matching ticker results")
    count: int = Field(..., description="Number of results returned", example=5)
    total_available: int = Field(..., description="Total number of tickers available", example=10000)

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "query": "apple",
                "results": [
                    {
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
                        "last_updated": "2023-01-15T10:30:00Z"
                    }
                ],
                "count": 1,
                "total_available": 10000
            }
        }