"""GET /datasets — catalog list. GET /datasets/{code}/slice-stats."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.atlas_api.schemas import DatasetSummary, SliceStats, YearAvailability
from src.api.agent_tools import agent_tool

router = APIRouter(tags=["atlas"])


def _stats_source(request: Request):
    src = request.app.state.fontem_stats_source
    if not src.configured:
        raise HTTPException(
            status_code=503,
            detail="stats store unavailable (STATS_DATABASE_URL unset)",
        )
    return src


@router.get("/datasets",
    openapi_extra=agent_tool(
        name="list_datasets",
        when=("the user asks what statistics exist, or you need a dataset code before "
              "reading values"),
        group="statistics"))
def list_datasets(request: Request) -> list[DatasetSummary]:
    """Every enabled dataset + its last successful sync.

    Slice stats are NOT embedded here — migration datasets have tens
    of thousands of dimension combinations and the embed pushed the
    response to 57 MB. Use `/datasets/{code}/slice-stats` to fetch
    bounds for a single dataset on demand.
    """
    return [DatasetSummary(**row) for row in _stats_source(request).list_datasets()]


@router.get("/datasets/{code}/slice-stats", response_model=list[SliceStats])
def list_slice_stats(code: str, request: Request) -> list[SliceStats]:
    """Per-(dimension-slice) value distribution for one dataset.

    Returns an empty list when the slice-stats table is missing or
    hasn't been backfilled yet — frontend falls back to per-data
    bounds and still renders the new palette + null layer.
    """
    rows = _stats_source(request).fetch_slice_stats(code)
    return [SliceStats(**row) for row in rows]


@router.get(
    "/datasets/{code}/availability",

    openapi_extra=agent_tool(
        name="dataset_year_coverage",
        when=("you need to know which years and regions a dataset actually covers before "
              "quoting a trend"),
        group="statistics"),
    response_model=list[YearAvailability],
)
def list_year_availability(code: str, request: Request) -> list[YearAvailability]:
    """Per-(nuts_level, slice, year) coverage for one dataset.

    Drives the Atlas "hide low-coverage years/datasets" toggles.
    Returns [] if the sidecar table is missing — the toggles then
    no-op rather than failing the dataset picker.
    """
    rows = _stats_source(request).fetch_year_availability(code)
    return [YearAvailability(**row) for row in rows]
