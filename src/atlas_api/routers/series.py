"""GET /series — time-series rows for one dataset."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request

from src.atlas_api.schemas import SeriesResponse

router = APIRouter(tags=["atlas"])


def _stats_source(request: Request):
    src = request.app.state.fontem_stats_source
    if not src.configured:
        raise HTTPException(
            status_code=503,
            detail="stats store unavailable (STATS_DATABASE_URL unset)",
        )
    return src


@router.get("/series", response_model=SeriesResponse)
# pylint: disable=too-many-arguments,too-many-positional-arguments
def fetch_series(
    request: Request,
    dataset: str = Query(..., description="Dataset code, e.g. nama_10r_3gdp"),
    geo: list[str] | None = Query(
        None,
        description=(
            "One or more NUTS codes. Mutually exclusive with `nuts_level`."
        ),
    ),
    nuts_level: int | None = Query(
        None, ge=0, le=3,
        description="Restrict to all geo codes at this NUTS level (0..3).",
    ),
    start: int | None = Query(None, description="Inclusive start year"),
    end: int | None = Query(None, description="Inclusive end year"),
    dimensions: str | None = Query(
        None, description='JSONB filter, e.g. {"sex":"T","age":"TOTAL"}',
    ),
) -> SeriesResponse:
    """Time-series rows for one dataset, filtered by geo or NUTS level."""
    if not geo and nuts_level is None:
        raise HTTPException(
            status_code=400,
            detail="must supply either `geo` or `nuts_level`",
        )
    dim_filter = None
    if dimensions:
        try:
            dim_filter = json.loads(dimensions)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"invalid dimensions JSON: {exc}",
            ) from exc

    settings = request.app.state.atlas_settings
    rows = _stats_source(request).fetch_series(
        dataset=dataset,
        geo=geo,
        nuts_level=nuts_level,
        start=start,
        end=end,
        dim_filter=dim_filter,
        row_limit=settings.series_row_limit,
    )
    return SeriesResponse(
        dataset=dataset,
        geo=geo,
        nuts_level=nuts_level,
        start=start,
        end=end,
        dimensions_filter=dim_filter,
        count=len(rows),
        truncated=len(rows) >= settings.series_row_limit,
        data=rows,
    )
