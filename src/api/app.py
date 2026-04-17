"""
GMR Stock Analysis — FastAPI Application
==========================================
Run with:
    python -m src.api.run           (production — loguru logging)
    uvicorn src.api.app:app --reload  (quick local dev)

Swagger UI:  http://localhost:8000/docs
ReDoc:       http://localhost:8000/redoc
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI, Request
from loguru import logger
from prometheus_fastapi_instrumentator import Instrumentator

from src.api.di import make_container

from src.api.routers.fundamentals import router as fundamentals_router
from src.api.routers.gmr import router
from src.api.routers.health import router as health_router
from src.api.routers.prices import router as prices_router
from src.api.routers.tickers import router as tickers_router
from src.api.routers.valuation import router as valuation_router
from src.api.routers.contracts import router as contracts_router
from src.api.routers.data_quality import router as data_quality_router
from src.api.routers.entity_resolution import router as entity_resolution_router
from src.api.routers.persons import router as persons_router
from src.api.routers.graph import router as graph_router
from src.api.routers.geo import router as geo_router


@asynccontextmanager
async def _lifespan(application: FastAPI):  # pylint: disable=unused-argument
    logger.info("GMR Stock Analysis API starting up…")
    yield
    logger.info("GMR Stock Analysis API shutting down…")


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
    root_path="/api",
    lifespan=_lifespan,
)


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """Log every HTTP request with method, path, status code, and duration.

    Health-probe traffic (/health) is logged at DEBUG only — it fires every
    few seconds from Kubernetes and adds no signal at INFO level.
    """
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    log = logger.debug if request.url.path.endswith("/health") else logger.info
    log(
        "{method} {path} → {status}  ({duration:.1f} ms)",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration=duration_ms,
    )
    return response


app.include_router(router)
app.include_router(tickers_router)
app.include_router(fundamentals_router)
app.include_router(valuation_router)
app.include_router(prices_router)
app.include_router(health_router)
app.include_router(contracts_router)
app.include_router(data_quality_router)
app.include_router(entity_resolution_router)
app.include_router(persons_router)
app.include_router(graph_router)
app.include_router(geo_router)

# Expose Prometheus metrics at /metrics (scraped by ServiceMonitor)
Instrumentator().instrument(app).expose(app)

# Wire dishka DI — single Neo4jClient shared by all data sources.
# Skipped during test imports (tests supply their own mock container).
if os.environ.get("NEO4J_URI") or os.environ.get("GMR_PRODUCTION"):
    _container = make_container()
    setup_dishka(_container, app)


@app.get("/health", tags=["Health"], include_in_schema=False)
async def health() -> dict:
    """Lightweight liveness/readiness probe — returns 200 immediately."""
    return {"status": "ok"}
