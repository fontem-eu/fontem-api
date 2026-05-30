"""GET /data-quality/etl-runs — recent ETL CronJob invocations.

Powers the data-quality dashboard's "last successful run per cronjob"
panel. One row per CronJob run, newest first. Optional ?cronjob_name=
and ?status= filters narrow the result, both backed by indexes on
events.etl_run.

Historically lived at ``/atlas/etl-runs`` inside ``src/atlas_api/``,
which made /atlas/health roll up the events-DB connection as if it
were an Atlas requirement. Moving here makes the dependency truthful:
the user-facing Atlas feature reads only from fontem-stats-postgres;
this endpoint reads only from events-postgres.

``EtlRunsSource`` is still attached on ``app.state.etl_runs_source``
by ``atlas_api.app._attach_state`` because Atlas owns the events-DB
connection wiring (FastAPI app state is global to the app, not the
feature). When the wider chart split lands the source will live in
its own module and Atlas will drop the import.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from src.atlas_api.schemas import EtlRun

router = APIRouter(prefix="/data-quality", tags=["data-quality"])


def _etl_runs_source(request: Request):
    src = request.app.state.etl_runs_source
    if not src.configured:
        raise HTTPException(
            status_code=503,
            detail="events store unavailable (EVENTS_DATABASE_URL unset)",
        )
    return src


@router.get("/etl-runs", response_model=list[EtlRun])
def list_etl_runs(
    request: Request,
    cronjob_name: str | None = Query(
        default=None,
        description="filter to a single cronjob (e.g. etl-gleif)",
    ),
    status: str | None = Query(
        default=None,
        description="filter to running | success | failed",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[EtlRun]:
    """Last `limit` ETL CronJob runs across the cluster.

    Returns an empty list when the table doesn't exist yet (pre-
    bootstrap cluster) — the dashboard renders "no runs recorded
    yet" rather than 500-ing.
    """
    src = _etl_runs_source(request)
    settings = request.app.state.atlas_settings
    capped = min(limit, settings.etl_runs_row_limit)
    rows = src.recent_runs(
        limit=capped, cronjob_name=cronjob_name, status=status,
    )
    return [EtlRun(**row) for row in rows]
