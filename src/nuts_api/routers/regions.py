"""The public NUTS reference endpoints.

Written for somebody else's code: stable shapes, explicit provenance, cache
headers, and a bulk download so nobody has to page through 1,798 regions to
get a copy.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from src.data import nuts_gazetteer
from src.nuts_api import index
from src.nuts_api.schemas import (
    Coverage, Region, RegionDetail, RegionPage, SearchResults, ServiceInfo, Source,
)

router = APIRouter()

# Reference data, rebuilt only when a NUTS vintage or a label source changes,
# so it is worth caching hard. Public and non-personal by construction: these
# are place names, identical for every caller.
_CACHE_CONTROL = "public, max-age=3600, stale-while-revalidate=86400"

SOURCES = [
    Source(
        id="eurostat",
        name="Eurostat GISCO — NUTS 2024",
        gives="the national-language name and its Latin transliteration, for every region",
        licence="Reuse permitted with attribution (Commission Decision 2011/833/EU)",
        attribution="© Eurostat (names). Administrative boundaries: © EuroGeographics.",
        url="https://ec.europa.eu/eurostat/web/nuts",
    ),
    Source(
        id="euvoc",
        name="EU Publications Office — country authority table and NUTS scheme",
        gives="official country names in the 24 languages, the code-succession "
              "chain, and Eurostat metro-region labels",
        licence="CC BY 4.0",
        attribution="© European Union, Publications Office",
        url="https://op.europa.eu/en/web/eu-vocabularies",
    ),
    Source(
        id="wikidata",
        name="Wikidata (property P605, NUTS code)",
        gives="region names and aliases in the 24 languages, where an item has them",
        licence="CC0 1.0",
        attribution="Wikidata contributors",
        url="https://www.wikidata.org/wiki/Property:P605",
    ),
]

LICENCE = (
    "CC BY 4.0. Attribute Fontem and the upstream sources listed in `sources`. "
    "These are region NAMES only — no boundary geometry is served here, so the "
    "EuroGeographics terms on GISCO geometry do not apply to this response."
)

ENDPOINTS = {
    "service": "GET /nuts",
    "list": "GET /nuts/regions?level=&country=&lang=&limit=&offset=",
    "one": "GET /nuts/regions/{code}",
    "search": "GET /nuts/search?q=&lang=&level=&country=&limit=",
    "bulk_json": "GET /nuts/gazetteer.json",
    "bulk_csv": "GET /nuts/regions.csv",
}


def _public(response: Response) -> None:
    """Cache + CORS headers.

    Cross-origin is opened deliberately and narrowly: every response here is
    the same public reference data for every caller, there is nothing to
    authorise, and a browser app that cannot read it cannot use this API at
    all.
    """
    doc = nuts_gazetteer.document()
    response.headers["Cache-Control"] = _CACHE_CONTROL
    response.headers["ETag"] = (
        f'W/"nuts-{doc.get("nuts_version", "?")}-{doc.get("generated", "?")}"')
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Vary"] = "Accept-Encoding"


@router.get("")
def service_info(response: Response) -> ServiceInfo:
    """What this API serves, where each name came from, and how complete it is.

    Read this first: coverage is uneven by nature. Eurostat publishes two
    names per region and nobody publishes 24, so the translations are
    assembled from the sources below and a region without one falls back to
    the Latin transliteration. `coverage` says exactly how far that goes,
    per language and per level, and every region carries its own `sources`.
    """
    _public(response)
    doc = nuts_gazetteer.document()
    coverage = dict(doc.get("coverage") or {})
    coverage.setdefault("regions", len(index.records()))
    coverage.setdefault("with_translations", 0)
    coverage.setdefault("average_languages", 0.0)
    coverage.setdefault("per_language", {})
    coverage.setdefault("per_level", {})
    return ServiceInfo(
        nuts_version=str(doc.get("nuts_version") or "unknown"),
        generated=str(doc.get("generated") or "unknown"),
        languages=list(nuts_gazetteer.languages()),
        coverage=Coverage(**{k: coverage[k] for k in
                            ("regions", "with_translations", "average_languages",
                             "per_language", "per_level")}),
        sources=SOURCES,
        licence=LICENCE,
        endpoints=ENDPOINTS,
    )


@router.get("/regions")
def list_regions(
    response: Response,
    level: Annotated[int | None, Query(ge=0, le=3, description="NUTS level")] = None,
    country: Annotated[str | None, Query(
        min_length=2, max_length=2,
        description="NUTS-0 code, e.g. EL. Note EL, not GR.")] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RegionPage:
    """Every region, with all of its known names. Filter, or take the lot.

    Ordered by code, so paging is stable. `limit` caps at 2000 — one page
    holds the whole classification if you ask for it, and
    `GET /nuts/gazetteer.json` is there for a copy you keep.
    """
    _public(response)
    rows = [r for r in index.records().values()
            if (level is None or r["level"] == level)
            and (not country or r["country"] == country.upper())]
    page = rows[offset:offset + limit]
    return RegionPage(total=len(rows), limit=limit, offset=offset,
                      regions=[Region(**r) for r in page])


@router.get("/regions.csv", response_class=Response)
def regions_csv(response: Response) -> Response:
    """The names as one long-format CSV: a row per region per name.

    `code,level,country,parent,language,name,kind` — the shape that loads
    into a spreadsheet or a table without anyone writing JSON-flattening
    code first. `language` is empty for the forms that have no language:
    the Eurostat pair and the aliases.
    """
    _public(response)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["code", "level", "country", "parent", "language", "name", "kind"])
    for rec in index.records().values():
        base = [rec["code"], rec["level"], rec["country"], rec["parent"] or ""]
        if rec["name_native"]:
            writer.writerow(base + ["", rec["name_native"], "native"])
        if rec["name_latn"] and rec["name_latn"] != rec["name_native"]:
            writer.writerow(base + ["", rec["name_latn"], "latn"])
        for lang in nuts_gazetteer.languages():
            if lang in rec["names"]:
                writer.writerow(base + [lang, rec["names"][lang], "name"])
        for alias in rec["aliases"]:
            writer.writerow(base + ["", alias, "alias"])
    return Response(content=buffer.getvalue(), media_type="text/csv",
                    headers=dict(response.headers) | {
                        "Content-Disposition": 'attachment; filename="nuts-names.csv"'})


@router.get("/gazetteer.json", response_class=Response)
def gazetteer(response: Response) -> Response:
    """The whole gazetteer as built, header and all — for vendoring.

    The same artifact this service reads, so a consumer who wants a local
    copy gets exactly what we serve, with its version, build date, coverage
    report and per-region provenance intact.
    """
    _public(response)
    return Response(content=json.dumps(nuts_gazetteer.document(), ensure_ascii=False),
                    media_type="application/json",
                    headers=dict(response.headers))


@router.get("/search")
def search_regions(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    response: Response,
    q: Annotated[str, Query(min_length=1, max_length=120,
                            description="A region name in any of the 24 "
                                        "languages, or a NUTS code")],
    lang: Annotated[str | None, Query(
        max_length=16, description="Language for the `name` field in each "
                                   "result, and the one whose names rank "
                                   "highest. Default English.")] = None,
    level: Annotated[int | None, Query(ge=0, le=3)] = None,
    country: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SearchResults:
    """Name → NUTS code, in any of the 24 languages, accent- and case-blind.

    This is the part Eurostat cannot answer: "Lisbonne", "Lissabon" and
    "Área Metropolitana de Lisboa" all resolve to `PT1A0`, and `Αττική`,
    "Attiki" and "Attica" all resolve to `EL3`. Each hit says which form
    matched and in which language, so a caller can show why.

    Region names, not settlement names: a NUTS 3 unit is matched by the
    metro region it belongs to where Eurostat names one ("Athina"), but
    there is no city gazetteer behind this.
    """
    _public(response)
    language = nuts_gazetteer.resolve_language(lang)
    total, matches = index.search(q, language, level=level,
                                 country=country, limit=limit)
    return SearchResults(query=q, lang=language, total=total, matches=matches)


@router.get("/regions/{code}",
            responses={404: {"description": "No region with that code in this "
                                            "NUTS vintage. Codes are retired and "
                                            "renumbered between vintages — see "
                                            "`nuts_version` on GET /nuts."}})
def one_region(code: str, response: Response) -> RegionDetail:
    """One region, its parent chain and its direct children.

    The hierarchy needs no extra call: NUTS codes nest by prefix, so the
    ancestors and children are derived rather than stored.
    """
    _public(response)
    rec = index.records().get(code.upper())
    if not rec:
        raise HTTPException(status_code=404, detail=f"no NUTS region {code!r} "
                                                   f"in this vintage")
    known = index.records()
    return RegionDetail(
        **rec,
        ancestors=[Region(**known[c]) for c in index.ancestors_of(rec["code"])],
        children=[Region(**known[c])
                  for c in sorted(index.children_of().get(rec["code"], []))],
    )
