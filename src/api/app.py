"""
GMR Stock Analysis — FastAPI Application
==========================================
Run with:
    uvicorn src.api.app:app --reload

Swagger UI:  http://localhost:8000/docs
ReDoc:       http://localhost:8000/redoc
"""
from __future__ import annotations

from fastapi import FastAPI

from src.api.routers.gmr import router
from src.api.routers.tickers import router as tickers_router

app = FastAPI(
    title="GMR Stock Analysis API",
    description=(
        "REST API for the GMR (Gonçalo Martins Rato) stock screening indicator.\n\n"
        "**GMR Long** evaluates a stock for long-term value investing using SEC EDGAR "
        "10-K fundamentals: P/E, P/B, ROE, Net Profit Margin, Debt/Equity, "
        "Dividend Yield, Quick Ratio and Free Cash Flow — averaged over N fiscal years.\n\n"
        "**GMR Short** evaluates a stock for short-term swing trading using 6 months of "
        "daily OHLCV data: win probability, VUp / VDown volatility, and a 43-day moving "
        "average trend signal.\n\n"
        "Add `?summarize=true` to any endpoint to receive a compact response "
        "containing only the `gmr_ratio` verdict object."
    ),
    version="0.1.0",
    contact={"name": "bemar-edgar", "email": "bemar-edgar@research.com"},
    license_info={"name": "MIT"},
)

app.include_router(router)
app.include_router(tickers_router)
