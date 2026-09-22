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
from typing import Annotated

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query, Request, Response

from src.analysis.geo_source import GeoSource
from src.data import eu_gate as eu_gate_policy
from src.data import geo_ip
from src.data import nuts_gazetteer
from src.nuts_api import index as nuts_index
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
    # The MaxMind/db-ip database keys countries by ISO alpha-2
    # (`country.iso_code`), so normalise before converting: feeding an
    # alpha-2 straight to alpha3_to_alpha2() matches nothing and returns
    # a null nuts0 for every visitor.
    a2 = geo_ip.country_for(ip) if ip else None
    a3 = LocationService.to_alpha3(a2) if a2 else None
    nuts0 = LocationService.alpha3_to_alpha2(a3) if a3 else None
    return {"country_alpha3": a3, "nuts0": nuts0}


_BOUNDARIES_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def _localise_labels(rows: list[dict], lang: str | None, key: str = "label") -> list[dict]:
    """Rewrite region labels in the caller's language, in place.

    The aggregate queries read `region.name` from the graph, which holds
    Eurostat's Latin transliteration — so a choropleth said "Attiki" and
    "Voreia Elláda" to a Greek reader and "Kentriki Elláda" to an English
    one, while the region picker two clicks away said "Attica Region". Same
    gazetteer, same answer everywhere. A code the gazetteer does not carry
    (a retired vintage still in the data) keeps the label it came with.
    """
    named = nuts_gazetteer.localised(nuts_gazetteer.resolve_language(lang))
    for row in rows:
        entry = named.get(row.get("nuts_code") or "")
        if entry:
            row[key] = entry["name"]
    return rows


@router.get(
    "/aggregate",
    openapi_extra=agent_tool(
        name="regional_aggregate",
        when="the user asks how much activity sits in a region rather than in one entity",
        group="geography"),
)
@inject
def aggregate(  # pylint: disable=too-many-arguments
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
    lang: Annotated[str | None, Query(
        max_length=16,
        description="Language for the region labels (EU-24 code). Default English.",
    )] = None,
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
        "regions": _localise_labels(rows, lang),
    }


@router.get("/entity/{entity_id}/aggregate")
@inject
def entity_aggregate(  # pylint: disable=too-many-arguments
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
    lang: Annotated[str | None, Query(
        max_length=16,
        description="Language for the region labels (EU-24 code). Default English.",
    )] = None,
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
        "regions": _localise_labels(rows, lang),
    }


@router.get("/nuts-regions", deprecated=True)
def nuts_regions(
    codes: Annotated[str | None, Query(max_length=2000)] = None,
    lang: Annotated[str | None, Query(max_length=16)] = None,
):
    """Deprecated — use ``GET /nuts/regions``.

    Kept so the web app keeps working across a deploy where the API rolls
    before it does; it delegates to the public surface rather than keeping a
    second implementation of the same lookup. Remove once nothing calls it.
    """
    wanted = None
    if codes is not None:
        wanted = {c.strip().upper() for c in codes.split(",") if c.strip()}
        if not wanted:
            return {"regions": [], "lang": nuts_gazetteer.resolve_language(lang)}
    language = nuts_gazetteer.resolve_language(lang)
    out = [
        {"code": rec["code"], "name": rec["name"], "level": rec["level"],
         "name_latn": rec["name_latn"], "name_native": rec["name_native"],
         "name_source": rec["name_source"]}
        for rec in (nuts_index.localised(r, language, with_names=False)
                    for r in nuts_index.records().values())
        if wanted is None or rec["code"] in wanted
    ]
    out.sort(key=lambda r: (r["level"], nuts_gazetteer.fold(r["name"])))
    return {"regions": out, "lang": language}


@router.get("/nuts-search-index", deprecated=True)
def nuts_search_index():
    """Deprecated — use ``GET /nuts/search``, which ranks server-side.

    This shipped a folded index for the client to match against, which meant
    two implementations of the same ranking and a folding contract that had
    to agree character for character across two languages. Kept only until
    the deployed web app stops asking for it.
    """
    return {"terms": nuts_gazetteer.search_index()}


def _regions_from_boundaries(wanted: set[str] | None) -> list[dict]:
    """Code/level/name straight from the bundled boundary files."""
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
            if code and (wanted is None or code.upper() in wanted):
                name = (props.get("name") or code).strip()
                out.append({"code": code, "name": name, "level": level,
                            "name_latn": "", "name_native": name,
                            "name_source": "native"})
    out.sort(key=lambda r: (r["level"], r["name"]))
    return out


@router.get("/nuts-boundaries")
def nuts_boundaries(
    level: int = Query(0, ge=0, le=3),
    lang: Annotated[str | None, Query(
        max_length=16,
        description="Language for feature names (EU-24 code). Default English.",
    )] = None,
):
    """Return bundled GeoJSON boundaries for a NUTS level.

    Feature names are localised from the gazetteer, with the Eurostat forms
    kept alongside as `name_native` and `name_latn`. The files themselves
    carry only the national-language name — and carry it with Eurostat's
    stray whitespace, so a map label read "Αττική " with a trailing space.
    """
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
    named = nuts_gazetteer.localised(nuts_gazetteer.resolve_language(lang))
    for feat in data.get("features", []):
        props = feat.get("properties") or {}
        code = props.get("nuts_code") or ""
        a3 = LocationService.alpha2_to_alpha3(code[:2]) if len(code) >= 2 else None
        if a3:
            props["country_a3"] = a3
        entry = named.get(code)
        if entry:
            props["name"] = entry["name"]
            props["name_native"] = entry["name_native"]
            props["name_latn"] = entry["name_latn"]
        elif props.get("name"):
            props["name"] = props["name"].strip()
    return data
