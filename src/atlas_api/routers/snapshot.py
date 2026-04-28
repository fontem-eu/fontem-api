"""GET /snapshot — choropleth-shaped slice for one (dataset, year, NUTS level).

Differs from /series by collapsing to one row per geo, optionally
filtered to a single dimension combination. Returns the available
combinations alongside, so the UI can render a slice picker without
a second request.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request

from src.atlas_api.schemas import SnapshotResponse

router = APIRouter(tags=["atlas"])


def _stats_source(request: Request):
    src = request.app.state.fontem_stats_source
    if not src.configured:
        raise HTTPException(
            status_code=503,
            detail="stats store unavailable (STATS_DATABASE_URL unset)",
        )
    return src


@router.get("/snapshot", response_model=SnapshotResponse)
def snapshot(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    request: Request,
    dataset: str = Query(..., description="Dataset code"),
    year: int = Query(..., description="Year (e.g. 2023)"),
    nuts_level: int = Query(..., ge=0, le=3, description="NUTS level 0..3"),
    dimensions: str | None = Query(
        None,
        description=(
            'JSONB filter selecting one slice, e.g. {"sex":"T","age":"TOTAL"}.'
            " Omit to receive `available_dim_combos` only when ambiguous."
        ),
    ),
) -> SnapshotResponse:
    """Single (dataset, year, NUTS level) slice for the choropleth."""
    dim_filter = None
    if dimensions:
        try:
            dim_filter = json.loads(dimensions)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"invalid dimensions JSON: {exc}",
            ) from exc

    cells, available = _stats_source(request).snapshot(
        dataset=dataset,
        year=year,
        nuts_level=nuts_level,
        dim_filter=dim_filter,
    )
    return SnapshotResponse(
        dataset=dataset,
        year=year,
        nuts_level=nuts_level,
        dimensions_filter=dim_filter,
        available_dim_combos=available,
        count=len(cells),
        cells=cells,
    )
