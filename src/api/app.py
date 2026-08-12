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

from src.data.graph.neo4j_client import Neo4jClient
from src.api import graph_schema
from src.api.routers.catalogue import router as catalogue_router
from src.api.routers.docs import router as docs_router
from src.api.routers.fundamentals import router as fundamentals_router
from src.api.routers.gmr import router
from src.api.routers.health import router as health_router
from src.api.routers.prices import router as prices_router
from src.api.routers.tickers import router as tickers_router
from src.api.routers.valuation import router as valuation_router
from src.api.routers.contracts import router as contracts_router
from src.api.routers.data_quality import router as data_quality_router
from src.api.routers.legislative_dq import router as legislative_dq_router
from src.api.routers.petitions import router as petitions_router
from src.api.routers.dq_assertions import router as dq_assertions_router
from src.api.routers.value_review import router as value_review_router
from src.api.routers.viz import router as viz_router
from src.api.routers.query import router as query_router
from src.api.routers.dq_etl_runs import router as dq_etl_runs_router
from src.api.routers.dq_pipeline import router as dq_pipeline_router
from src.api.routers.entity_resolution import router as entity_resolution_router
from src.api.routers.persons import router as persons_router
from src.api.routers.graph import router as graph_router
from src.api.routers.geo import router as geo_router
from src.api.routers.mentions import router as mentions_router
from src.api.routers.euro_tracker import router as euro_tracker_router
from src.api.routers.sparql import router as sparql_router
from src.api.routers.search import router as search_router
from src.atlas_api import build_router as build_atlas_router
from src.atlas_api.app import _attach_state as attach_atlas_state


@asynccontextmanager
async def _lifespan(application: FastAPI):  # pylint: disable=unused-argument
    logger.info("GMR Stock Analysis API starting up…")
    # The full-text index /search depends on. Idempotent, and a no-op where
    # it already exists — it existed only in production until now, which is
    # why a deploy that used it returned zero results everywhere else.
    try:
        container = application.state.dishka_container
        async with container() as request_container:
            neo4j = await request_container.get(Neo4jClient)
            graph_schema.ensure_indexes(neo4j)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("graph index check skipped: {}", exc)
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
app.include_router(docs_router)
app.include_router(contracts_router)
app.include_router(data_quality_router)
app.include_router(catalogue_router)
app.include_router(legislative_dq_router)
app.include_router(petitions_router)
app.include_router(dq_assertions_router)
app.include_router(value_review_router)
app.include_router(viz_router)
app.include_router(query_router)
app.include_router(dq_etl_runs_router)
app.include_router(dq_pipeline_router)
app.include_router(entity_resolution_router)
app.include_router(persons_router)
app.include_router(graph_router)
app.include_router(geo_router)
app.include_router(mentions_router)
app.include_router(euro_tracker_router)
app.include_router(sparql_router)
app.include_router(search_router)

# Atlas API — mounted under /atlas as a self-contained module.
# `attach_atlas_state` stashes per-source connection state on `app.state`
# so the Atlas routers can reach it. Designed to be lift-and-shipped to
# a standalone service later — see src/atlas_api/README.md.
attach_atlas_state(app)
app.include_router(build_atlas_router(), prefix="/atlas", tags=["atlas"])

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
