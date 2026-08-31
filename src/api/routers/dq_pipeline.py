"""GET /data-quality/pipeline — per-source pipeline health.

Joins the DataSource registry (producer ⇄ cronjob ⇄ dashboard route)
against events-DB metrics so the data-quality hub can show, per source,
a freshness / dead-letter / volume KPI at a glance. Reads only from
events-postgres (via the shared EtlRunsSource); never touches Neo4j.
"""
from __future__ import annotations

from datetime import datetime, timezone

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from src.api.dq_sources import BY_ID, DATA_SOURCES
from src.atlas_api.schemas import ConsumerLag, SourcePipelineHealth

router = APIRouter(prefix="/data-quality", tags=["data-quality"])

# A source with no successful run / event inside this window is "stale".
# Most feeds are daily or weekly; 48h flags a daily feed that missed two
# runs without false-positiving a weekly one between runs (those carry a
# fresh last_run_finished even when no new events land).
_STALE_AFTER_HOURS = 48.0


@router.get(
    "/pipeline",
    responses={503: {"description": "events store unavailable"}},
)
def pipeline_health(request: Request) -> list[SourcePipelineHealth]:
    """Per-source pipeline health for every registered DataSource."""
    src = request.app.state.etl_runs_source
    if not src.configured:
        raise HTTPException(
            status_code=503,
            detail="events store unavailable (EVENTS_DATABASE_URL unset)",
        )
    metrics = src.pipeline_metrics()
    by_producer = metrics["by_producer"]
    by_cronjob = metrics["by_cronjob"]
    now = datetime.now(timezone.utc)

    out: list[SourcePipelineHealth] = []
    for source in DATA_SOURCES:
        prod = by_producer.get(source.producer, {})
        cron = by_cronjob.get(source.cronjob, {})

        events_total = prod.get("events_total", 0) or 0
        deadletter = prod.get("deadletter", 0) or 0
        # Freshness anchor: prefer the last *successful* run's finish, else
        # fall back to the newest event emitted by the producer.
        finished = cron.get("last_run_finished_at")
        anchor = finished or prod.get("last_event_at")
        age_hours = (
            (now - anchor).total_seconds() / 3600 if anchor is not None else None
        )

        out.append(SourcePipelineHealth(
            id=source.id,
            label=source.label,
            theme=source.theme,
            route=source.route,
            events_total=events_total,
            events_30d=prod.get("events_30d", 0) or 0,
            last_event_at=prod.get("last_event_at"),
            last_run_at=cron.get("last_run_at"),
            last_run_finished_at=finished,
            last_run_status=cron.get("last_run_status"),
            last_run_summary=cron.get("last_run_summary"),
            deadletter=deadletter,
            deadletter_pct=(
                round(deadletter / events_total * 100, 3) if events_total else 0.0
            ),
            age_hours=round(age_hours, 1) if age_hours is not None else None,
            stale=anchor is None or (
                age_hours is not None and age_hours > _STALE_AFTER_HOURS
            ),
        ))
    return out


@router.get(
    "/pipeline/{source_id}/timeline",
    responses={
        404: {"description": "unknown source"},
        503: {"description": "events store unavailable"},
    },
)
def source_events_timeline(
    request: Request,
    source_id: str,
    days: Annotated[int, Query(ge=1, le=365)] = 90,
) -> list[dict]:
    """Events-per-day for one registered source over the last `days`.

    Powers the per-dashboard volume-over-time panel. ``day`` is an ISO
    date; ``events`` the count emitted by that source's producer.
    """
    source = BY_ID.get(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"unknown source: {source_id}")
    src = request.app.state.etl_runs_source
    if not src.configured:
        raise HTTPException(
            status_code=503,
            detail="events store unavailable (EVENTS_DATABASE_URL unset)",
        )
    rows = src.events_timeline(source.producer, days=days)
    return [
        {"day": r["day"].isoformat(), "events": r["events"]} for r in rows
    ]


@router.get(
    "/consumer-lag",
    responses={503: {"description": "events store unavailable"}},
)
def consumer_lag(request: Request) -> list[ConsumerLag]:
    """How far each event consumer trails the head of the log.

    The sinks and the consolidator trigger all read the same event
    stream at their own pace, so a single number per consumer says
    whether the graph, the triple store and the search index are
    actually current — something no per-source freshness check can
    show, because a source can be ingesting perfectly while the
    consumer writing it into Neo4j has stalled.
    """
    src = request.app.state.etl_runs_source
    if not src.configured:
        raise HTTPException(
            status_code=503,
            detail="events store unavailable (EVENTS_DATABASE_URL unset)",
        )
    return [ConsumerLag(**row) for row in src.consumer_lag()]
