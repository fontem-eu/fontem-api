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

from src.api.agent_tools import agent_tool

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
    """Cache + cross-origin headers.

    Cross-origin is opened deliberately and narrowly: every response here is
    the same public reference data for every caller, there is nothing to
    authorise, and a browser app that cannot read it cannot use this API at
    all.

    The wildcard is safe for a reason worth keeping in view. ``*`` and
    credentials are mutually exclusive by specification: a browser will not
    attach cookies or ``Authorization`` to a request answered with ``*``, and
    rejects the response outright if it also claims
    ``Access-Control-Allow-Credentials: true``. So no third-party page can read
    anything user-specific through these routes — and there is nothing
    user-specific here to read. That holds only while this is the ONE place
    the header is set and credentials never appear beside it;
    ``tests/test_nuts_cors_scope.py`` enforces both, so a route that copies
    this helper, or a blanket ``CORSMiddleware``, fails CI rather than review.

    ``Cross-Origin-Resource-Policy: cross-origin`` declares the same intent to
    the browser's other isolation model, so a cross-origin-isolated page
    (COEP ``require-corp``) can load these responses too, instead of the
    intent having to be inferred from CORS alone.
    """
    doc = nuts_gazetteer.document()
    response.headers["Cache-Control"] = _CACHE_CONTROL
    response.headers["ETag"] = (
        f'W/"nuts-{doc.get("nuts_version", "?")}-{doc.get("generated", "?")}"')
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
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


@router.get(
    "/regions",
    openapi_extra=agent_tool(
        name="list_nuts_regions",
        when="you need the NUTS code for a place before asking for regional "
             "statistics, and a name search has not already given it to you",
        group="geography",
        params=("codes", "level", "max_level", "country", "lang")),
)
def list_regions(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    response: Response,
    level: Annotated[int | None, Query(ge=0, le=3, description="Exact NUTS level")] = None,
    max_level: Annotated[int | None, Query(
        ge=0, le=3, description="Deepest level to return; 0 is countries only")] = None,
    country: Annotated[str | None, Query(
        min_length=2, max_length=2,
        description="NUTS-0 code, e.g. EL. Note EL, not GR.")] = None,
    codes: Annotated[str | None, Query(
        max_length=2000,
        description="Comma-separated codes; returns only these. For labelling "
                    "a handful without pulling the classification.")] = None,
    lang: Annotated[str | None, Query(
        max_length=16, description="Language for `name` on each row (EU-24 "
                                   "code). Unknown or missing means "
                                   "English.")] = None,
    names: Annotated[str, Query(
        pattern="^(all|none)$",
        description="`none` drops the per-language map and the aliases, "
                    "which are most of the payload, for a caller that only "
                    "needs the one language it asked for.")] = "all",
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RegionPage:
    """Every region, with all of its known names. Filter, or take the lot.

    Ordered by code, so paging is stable. The cap of 5000 is above the size
    of the classification on purpose: one call gets everything, and
    `names=none&lang=xx` roughly quarters that call when only the one
    language is wanted.
    """
    _public(response)
    wanted = None
    if codes is not None:
        wanted = {c.strip().upper() for c in codes.split(",") if c.strip()}
        # An explicit empty selection means "none", not "everything".
        if not wanted:
            return RegionPage(total=0, limit=limit, offset=offset, regions=[])
    language = nuts_gazetteer.resolve_language(lang)
    rows = [r for r in index.records().values()
            if (level is None or r["level"] == level)
            and (max_level is None or r["level"] <= max_level)
            and (not country or r["country"] == country.upper())
            and (wanted is None or r["code"] in wanted)]
    page = rows[offset:offset + limit]
    return RegionPage(
        total=len(rows), limit=limit, offset=offset,
        regions=[Region(**index.localised(r, language, with_names=names == "all"))
                 for r in page])


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


@router.get(
    "/search",
    openapi_extra=agent_tool(
        name="find_nuts_region",
        when="the user names a place — in any language — and you need the "
             "NUTS code for it",
        group="geography",
        params=("q", "lang", "level", "max_level", "country"),
        core=True),
)
def search_regions(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    response: Response,
    q: Annotated[str, Query(min_length=1, max_length=120,
                            description="A region name in any of the 24 "
                                        "languages, or a NUTS code")],
    lang: Annotated[str | None, Query(
        max_length=16, description="Language for the `name` field in each "
                                   "result, and the one whose names rank "
                                   "highest. Default English.")] = None,
    level: Annotated[int | None, Query(ge=0, le=3,
                                      description="Exact NUTS level")] = None,
    max_level: Annotated[int | None, Query(
        ge=0, le=3, description="Deepest level to return; 0 is countries only")] = None,
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
    total, matches = index.search(
        q, language, limit=limit,
        filters=index.Filters(level=level, max_level=max_level, country=country))
    return SearchResults(query=q, lang=language, total=total, matches=matches)


@router.get("/regions/{code}",
            responses={404: {"description": "No region with that code in this "
                                            "NUTS vintage. Codes are retired and "
                                            "renumbered between vintages — see "
                                            "`nuts_version` on GET /nuts."}})
def one_region(
    code: str,
    response: Response,
    lang: Annotated[str | None, Query(
        max_length=16, description="Language for `name` here and on the "
                                   "ancestors and children")] = None,
) -> RegionDetail:
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
    language = nuts_gazetteer.resolve_language(lang)
    return RegionDetail(
        **index.localised(rec, language),
        ancestors=[Region(**index.localised(known[c], language))
                   for c in index.ancestors_of(rec["code"])],
        children=[Region(**index.localised(known[c], language))
                  for c in sorted(index.children_of().get(rec["code"], []))],
    )
