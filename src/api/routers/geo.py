"""
Geo API Router
===============
Endpoints for geographic aggregation over the NUTS hierarchy.

- ``GET /geo/aggregate`` — aggregate entities/contracts by NUTS region.
- ``GET /geo/nuts-boundaries`` — return bundled GeoJSON for a NUTS level.

Boundary geometry is bundled in the image (``src/api/data/nutsN.geojson``)
because Eurostat's GISCO HTTPS endpoint is unreachable from the cluster.
Only NUTS 0 (country) boundaries ship today; NUTS 1–3 will follow once the
data is sourced.
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


@router.get("/nuts-boundaries")
def nuts_boundaries(
    level: int = Query(0, ge=0, le=3),
):
    """Return bundled GeoJSON boundaries for a NUTS level."""
    path = os.path.abspath(os.path.join(_BOUNDARIES_DIR, f"nuts{level}.geojson"))
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=501,
            detail=(
                f"Boundaries for NUTS {level} are not bundled yet. "
                f"Only NUTS 0 is available today."
            ),
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
