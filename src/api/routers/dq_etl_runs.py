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

from typing import Annotated

from fastapi import APIRouter, Query, Request

from src.api.helpers import events_source_or_503
from src.atlas_api.schemas import CronjobRuns, EtlRun

router = APIRouter(prefix="/data-quality", tags=["data-quality"])


def _etl_runs_source(request: Request):
    return events_source_or_503(request)


@router.get(
    "/etl-runs",
    responses={503: {"description": "events store unavailable"}},
)
def list_etl_runs(
    request: Request,
    cronjob_name: Annotated[str | None, Query(
        description="filter to a single cronjob (e.g. etl-gleif)",
    )] = None,
    status: Annotated[str | None, Query(
        description="filter to running | success | failed",
    )] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
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


@router.get(
    "/etl-runs/by-cronjob",
    responses={503: {"description": "events store unavailable"}},
)
def runs_by_cronjob(
    request: Request,
    per_job: Annotated[int, Query(
        ge=1, le=20,
        description="how many recent runs to return for each cronjob",
    )] = 4,
) -> list[CronjobRuns]:
    """The last `per_job` runs of every cronjob, grouped by cronjob.

    `/etl-runs` returns a flat newest-first window, which is the wrong
    shape for "is each job healthy": a cronjob running every 4 hours
    crowds out one running monthly, and the monthly job — the one whose
    silence matters most — drops off the list. Partitioning per cronjob
    gives every job the same visibility regardless of cadence.
    """
    src = _etl_runs_source(request)
    rows = src.recent_runs_by_cronjob(per_job=per_job)
    grouped: dict[str, list[EtlRun]] = {}
    for row in rows:
        grouped.setdefault(row["cronjob_name"], []).append(EtlRun(**row))
    return [
        CronjobRuns(cronjob_name=name, runs=runs)
        for name, runs in sorted(grouped.items())
    ]
