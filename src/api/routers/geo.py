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
from fastapi import APIRouter, HTTPException, Query, Request, Response

from src.analysis.geo_source import GeoSource
from src.data import eu_gate as eu_gate_policy
from src.data import geo_ip
from src.services.location_service import LocationService


from src.api.agent_tools import agent_tool

router = APIRouter(prefix="/geo", tags=["geo"])

_EU_GATE_DENY_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><title>Fontem — regional availability</title>
<style>body{font-family:system-ui,sans-serif;display:grid;place-items:center;
min-height:100vh;margin:0;background:#0e1116;color:#e6e8eb}
main{max-width:32rem;padding:2rem;text-align:center}
h1{font-size:1.3rem}p{color:#9aa4b2;line-height:1.6}</style></head><body>
<main><h1>Fontem is available from the European statistical space</h1>
<p>This platform serves the EU, EEA/EFTA, enlargement countries and the
UK. If you believe you are seeing this in error, your network may be
routing traffic from outside the region.</p></main></body></html>"""


@router.get("/eu-gate", include_in_schema=False)
def eu_gate(request: Request) -> Response:
    """Traefik forwardAuth target gating the public fontem.eu ingress.

    204 admits the request; the 403 body is served to the visitor
    verbatim by Traefik. Decision logic (and its fail-open stance)
    lives in src.data.eu_gate.
    """
    ip = geo_ip.client_ip_from(
        request.headers.get("x-forwarded-for"),
        request.headers.get("x-real-ip"),
        request.client.host if request.client else None,
    )
    if eu_gate_policy.is_allowed(ip):
        return Response(status_code=204)
    return Response(content=_EU_GATE_DENY_HTML, status_code=403,
                    media_type="text/html")


@router.get("/client-language")
def client_language(request: Request, response: Response) -> dict:
    """Coarse first-visit language hint from the caller's IP country.

    The SPA calls this only when the visitor has no stored language
    preference. Country-level only, resolved against a local database —
    the IP is not logged or stored, and the response is uncacheable so
    proxies can't leak one visitor's hint to another.
    """
    response.headers["Cache-Control"] = "no-store, private"
    ip = geo_ip.client_ip_from(
        request.headers.get("x-forwarded-for"),
        request.headers.get("x-real-ip"),
        request.client.host if request.client else None,
    )
    country = geo_ip.country_for(ip) if ip else None
    return {
        "country": country,
        "lang": geo_ip.language_for_country(country),
    }


@router.get("/client-region")
def client_region(request: Request, response: Response) -> dict:
    """Coarse home-region guess (NUTS-0 country) from the caller's IP.

    Seeds the profile "where you're from" default when the user hasn't set a
    region. Country-level only, resolved against a local database — the IP is
    not logged or stored, and the response is uncacheable so proxies can't
    leak one visitor's guess to another. Returns alpha-3 plus the NUTS-0
    (alpha-2) code the region picker uses (GRC -> EL).
    """
    response.headers["Cache-Control"] = "no-store, private"
    ip = geo_ip.client_ip_from(
        request.headers.get("x-forwarded-for"),
        request.headers.get("x-real-ip"),
        request.client.host if request.client else None,
    )
    a3 = geo_ip.country_for(ip) if ip else None
    nuts0 = LocationService.alpha3_to_alpha2(a3) if a3 else None
    return {"country_alpha3": a3, "nuts0": nuts0}


_BOUNDARIES_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


@router.get(
    "/aggregate",
    openapi_extra=agent_tool(
        name="regional_aggregate",
        when="the user asks how much activity sits in a region rather than in one entity",
        group="geography"),
)
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


@router.get(
    "/nuts-regions",
    openapi_extra=agent_tool(
        name="list_nuts_regions",
        when="you need the NUTS code for a place before asking for regional statistics",
        group="geography"),
)
def nuts_regions():
    """Flat, geometry-free list of NUTS regions across all bundled levels.

    Returns ``{regions: [{code, name, level}]}`` — small enough (~1.8k rows)
    to power a client-side cascading region picker without downloading the
    full boundary GeoJSON. Levels/children are derivable from the codes
    (a child's code is prefixed by its parent's).
    """
    out = []
    for level in range(4):
        path = os.path.abspath(
            os.path.join(_BOUNDARIES_DIR, f"nuts{level}.geojson")
        )
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for feat in data.get("features", []):
            props = feat.get("properties") or {}
            code = props.get("nuts_code")
            if code:
                out.append(
                    {"code": code, "name": props.get("name") or code, "level": level}
                )
    out.sort(key=lambda r: (r["level"], r["name"]))
    return {"regions": out}


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
        data = json.load(fh)
    # Enrich each feature with the country's alpha-3 code (derived from the NUTS
    # 2-letter prefix). Alpha-3 is the platform's canonical country key, so this
    # lets alpha-3 datasets join to boundaries — not only NUTS codes.
    for feat in data.get("features", []):
        code = (feat.get("properties") or {}).get("nuts_code") or ""
        a3 = LocationService.alpha2_to_alpha3(code[:2]) if len(code) >= 2 else None
        if a3:
            feat["properties"]["country_a3"] = a3
    return data
