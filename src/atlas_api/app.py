"""FastAPI factories for the Atlas API.

Two flavours, same routes:

- ``build_router()`` returns an APIRouter that the main fontem-api app
  mounts under ``/atlas``. Sources are stashed on the parent app's
  state at mount time so all the Atlas routers share one connection
  surface.
- ``build_app()`` wraps the same routers in a standalone FastAPI app
  for ``python -m src.atlas_api`` and any future split-out deployment.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI

from src.atlas_api import config as atlas_config
from src.atlas_api.routers import datasets, health, series
from src.atlas_api.sources.etl_runs import EtlRunsSource
from src.atlas_api.sources.fontem_stats import FontemStatsSource


def _attach_state(app: FastAPI) -> None:
    """Attach Atlas + cross-feature sources to ``app.state``.

    Atlas-only state lives in ``atlas_sources`` — those are the sources
    /atlas/health rolls up. ``etl_runs_source`` is stashed separately
    because it is not an Atlas source: it backs the /data-quality/etl-runs
    endpoint (one row per cronjob invocation, written by every fontem
    ETL via ``fontem_events.RunLog``). It was historically bundled into
    src/atlas_api/ for code-organisation convenience, but it has no
    semantic dependency on the Atlas feature. Bundling it into
    ``atlas_sources`` made /atlas/health flip to ``degraded`` whenever
    EVENTS_DATABASE_URL was absent, which is what caused smoke
    ATLAS-19/20/21/22 to skip on otherwise-healthy environments.
    """
    settings = atlas_config.load()
    fontem = FontemStatsSource(settings.stats_database_url)
    # Idempotent forward-migrations (slice-stats table on already-
    # deployed clusters). Best-effort: list_datasets falls back to
    # an empty slice_stats array if this no-ops on a read-only role.
    fontem.migrate()
    # Events DB is bootstrapped out-of-chart (gitops/infra/*); the
    # source is read-only, never runs DDL.
    etl_src = EtlRunsSource(settings.events_database_url)
    app.state.atlas_settings = settings
    app.state.fontem_stats_source = fontem
    app.state.etl_runs_source = etl_src
    # ONLY Atlas-required sources here. etl_runs_source intentionally
    # excluded — see docstring.
    app.state.atlas_sources = [fontem]


def build_router() -> APIRouter:
    """Mountable router for the main fontem-api app.

    Caller must run ``_attach_state`` against its own app — see
    ``mount_into(parent_app)`` for the typical wiring. The
    /data-quality/etl-runs router is registered separately by the
    parent app (src/api/app.py).
    """
    parent = APIRouter()
    parent.include_router(health.router)
    parent.include_router(datasets.router)
    parent.include_router(series.router)
    return parent


def mount_into(parent_app: FastAPI, prefix: str = "/atlas") -> None:
    """Mount Atlas routes on an existing FastAPI app.

    Stashes per-source state on `parent_app.state` so the routers can
    reach it via `request.app.state.fontem_stats_source` etc.
    """
    _attach_state(parent_app)
    parent_app.include_router(build_router(), prefix=prefix, tags=["atlas"])


def build_app() -> FastAPI:
    """Standalone Atlas service — used by ``python -m src.atlas_api``.

    No prefix: routes live at the root so a future Ingress can rewrite
    or front it without leaking the implementation path.
    """
    app = FastAPI(
        title="Fontem Atlas API",
        description=(
            "Read surface for the Fontem Atlas frontend — datasets and "
            "time-series over the curated Eurostat catalog (and future "
            "overlay sources)."
        ),
        version="0.1.0",
    )
    _attach_state(app)
    app.include_router(build_router())
    return app
