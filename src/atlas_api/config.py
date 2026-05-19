"""Atlas API configuration via environment variables.

All knobs live here so a future standalone deployment only needs to
set the same env vars (no other config knobs hidden in the routers).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime knobs sourced from environment variables."""

    stats_database_url: str | None
    """DSN for the fontem_stats Postgres. None → /atlas/health flags
    the source as `unconfigured`; routers that need the DB return 503."""

    events_database_url: str | None
    """DSN for the events Postgres (gmr_app database). Powers
    /atlas/etl-runs (the data-quality ETL execution log). None →
    endpoint returns 503; the other Atlas routes are unaffected."""

    series_row_limit: int = 100_000
    """Hard cap on /atlas/series result size — protects the API from
    a NUTS-3 × all-years × all-dim-combos request blowing memory."""

    etl_runs_row_limit: int = 200
    """Hard cap on /atlas/etl-runs result size — dashboard only
    needs the last ~50–100 across 15 cronjobs."""


def load() -> Settings:
    """Build a Settings instance from the current process environment."""
    return Settings(
        stats_database_url=os.environ.get("STATS_DATABASE_URL"),
        events_database_url=os.environ.get("EVENTS_DATABASE_URL"),
        series_row_limit=int(os.environ.get("ATLAS_SERIES_ROW_LIMIT", "100000")),
        etl_runs_row_limit=int(os.environ.get("ATLAS_ETL_RUNS_ROW_LIMIT", "200")),
    )
