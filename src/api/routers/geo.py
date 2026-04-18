"""
Geo API Router
===============
Endpoints for geographic aggregation over the NUTS hierarchy.

- ``GET /geo/aggregate`` — aggregate entities/contracts by NUTS region.
- ``GET /geo/entity/{entity_id}/aggregate`` — entity-scoped contract map.
- ``GET /geo/nuts-boundaries`` — return bundled GeoJSON for a NUTS level.

Boundary geometry is bundled in the image (``src/api/data/nutsN.geojson``)
for all four NUTS levels (0–3).
"""
from __future__ import annotations

import json
import os

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query

from src.analysis.geo_source import GeoSource


router = APIRouter(prefix="/geo", tags=["geo"])

_BOUNDARIES_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


@router.get("/aggregate")
@inject
def aggregate(
    level: int = Query(0, ge=0, le=3, description="NUTS level (0–3)"),
    metric: str = Query(
        "companies",
        description="Metric to aggregate: companies, contracts, or contracts_eur",
    ),
    scope_nuts: str | None = Query(
        None,
        description="Required when level=3 — a NUTS 1 ancestor to cap query size",
    ),
    connected_to_country: str | None = Query(
        None,
        description=(
            "Alpha-3 country code. Restrict to entities with a graph path to "
            "any entity of that country (e.g. RUS for geopolitical queries)."
        ),
    ),
    *,
    source: FromDishka[GeoSource],
):
    """Aggregate a metric across NUTS regions at the requested level."""
    try:
        rows = source.aggregate_by_nuts(
            level=level,
            metric=metric,
            scope_nuts=scope_nuts,
            connected_to_country=connected_to_country,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "level": level,
        "metric": metric,
        "scope_nuts": scope_nuts,
        "connected_to_country": connected_to_country,
        "regions": rows,
    }


@router.get("/entity/{entity_id}/aggregate")
@inject
def entity_aggregate(
    entity_id: str,
    level: int = Query(0, ge=0, le=3, description="NUTS level (0–3)"),
    metric: str = Query(
        "contracts",
        description="Metric: contracts (count) or contracts_eur (EUR sum)",
    ),
    scope_nuts: str | None = Query(
        None,
        description=(
            "Ancestor NUTS code — restrict results to regions whose code "
            "starts with this prefix (e.g. 'DE' for all German regions)."
        ),
    ),
    *,
    source: FromDishka[GeoSource],
):
    """Aggregate one entity's contract volume by NUTS region.

    Works for both Company (gmr_id) and Authority (authority_id).
    """
    try:
        rows = source.aggregate_entity_by_nuts(
            entity_id=entity_id,
            level=level,
            metric=metric,
            scope_nuts=scope_nuts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "entity_id": entity_id,
        "level": level,
        "metric": metric,
        "scope_nuts": scope_nuts,
        "regions": rows,
    }


@router.get("/nuts-boundaries")
def nuts_boundaries(
    level: int = Query(0, ge=0, le=3),
):
    """Return bundled GeoJSON boundaries for a NUTS level."""
    path = os.path.abspath(os.path.join(_BOUNDARIES_DIR, f"nuts{level}.geojson"))
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=501,
            detail=f"Boundaries for NUTS {level} are not bundled yet.",
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
