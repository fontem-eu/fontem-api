"""Unified faceted search across the knowledge graph.

This backs the dedicated ``/search`` results page in the web app (distinct
from the header autocomplete, which stays on ``GET /api/search`` in
``contracts.py``). Where the autocomplete returns a small type-grouped
payload for a dropdown, this endpoint is built for a results page:

  * many entity types — companies, public bodies (authorities), people,
    lobbyists, procurement contracts, cohesion projects, sanctioned
    entities, EU legislation — selectable via the ``types`` facet;
  * advanced filters — ``country`` (alpha-3), ``nuts`` (NUTS-code prefix),
    and a ``date_from``/``date_to`` range, each applied only to the types
    that carry the relevant property;
  * pagination — a flat, relevance-ranked result list plus per-type counts
    for the facet sidebar.

Match semantics stay ``toLower(...) CONTAINS`` for parity with the existing
autocomplete (a full-text-index upgrade is tracked separately); each type
carries a small rank so exact/prefix hits sort above mid-string ones.
Legislation is the exception: it matches expression titles in the CELLAR
mirror via Virtuoso's full-text index, after the query is reduced to
content-bearing keywords by the linguistics service (stop-word removal).

Property names are the REAL materialized Neo4j properties (verified against
prod), which differ from the ETL event kwargs — notably lobbyists store
``detail_name``/``detail_acronym`` and cohesion projects live on
``:Disclosure {system:'eu-cohesion'}`` with ``detail_*`` fields.
"""
from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query

from src.api.lang import authority_name_expr, safe_lang
from src.data.graph.neo4j_client import Neo4jClient
from src.data.linguistics.client import LinguisticsClient
from src.data.sparql.virtuoso_client import SparqlTimeout, VirtuosoClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

# The entity types this endpoint can return, in the priority order used to
# break ties when merging the per-type result lists into one ranked page.
ALL_TYPES: tuple[str, ...] = (
    "company",
    "authority",
    "person",
    "lobbyist",
    "contract",
    "cohesion",
    "sanction",
    "legislation",
)

# Per-type cap. We over-fetch (offset+limit+1) up to this ceiling so we can
# merge/sort/slice deterministically and know whether more results exist,
# without paying for an unbounded global COUNT over 3.6M companies.
_MAX_PER_TYPE = 200


def _parse_types(types: str | None) -> list[str]:
    """Parse the comma-separated ``types`` facet into a validated list.

    Unknown tokens are dropped; an empty/absent value means "all types".
    """
    if not types:
        return list(ALL_TYPES)
    wanted = [t.strip().lower() for t in types.split(",") if t.strip()]
    picked = [t for t in ALL_TYPES if t in wanted]
    return picked or list(ALL_TYPES)


def _date_clause(prop: str, has_from: bool, has_to: bool) -> str:
    """Build an inclusive ISO date-range WHERE fragment for ``prop``.

    ISO ``yyyy-mm-dd`` strings compare lexicographically, so a plain string
    ``>=``/``<=`` is a correct range test. Returns "" when no bound is set.
    """
    parts = []
    if has_from:
        parts.append(f"{prop} >= $date_from")
    if has_to:
        parts.append(f"{prop} <= $date_to")
    return (" AND " + " AND ".join(parts)) if parts else ""


def _ctx(*parts) -> str:
    """Join non-empty context fragments with a middot separator."""
    return " · ".join(
        str(p).strip() for p in parts if p and str(p).strip()
    )


def _clip(text, limit: int = 130) -> str:
    """Collapse whitespace and trim a long description for a card."""
    if not text:
        return ""
    joined = " ".join(str(text).split())
    return joined if len(joined) <= limit else joined[: limit - 1].rstrip() + "…"


def _companies(session, params, want_geo_filter):
    """Companies by name. Optional country / NUTS-region geo filter."""
    geo = ""
    if params.get("country"):
        geo += (
            " AND (toLower(c.country) = toLower($country) "
            "OR toLower(coalesce(c.hq_country,'')) = toLower($country))"
        )
    if params.get("nuts"):
        # via LOCATED_IN region code prefix (populated for NUTS-0 today,
        # sub-country once the 1-3 load lands) OR the company's own region.
        geo += (
            " AND (EXISTS { MATCH (c)-[:LOCATED_IN]->(r:NUTSRegion) "
            "WHERE r.code STARTS WITH $nuts } "
            "OR toUpper(coalesce(c.region,'')) STARTS WITH $nuts "
            "OR toUpper(coalesce(c.hq_region,'')) STARTS WITH $nuts)"
        )
    if not want_geo_filter:
        geo = ""
    rows = session.run(
        "MATCH (c:Company) "
        "WHERE c.name IS NOT NULL AND toLower(c.name) CONTAINS toLower($q) "
        "  AND NOT toLower(trim(coalesce(c.name,''))) IN "
        "      ['nan','','n/a','none','null','-'] "
        f"{geo} "
        "OPTIONAL MATCH (c)-[:LISTED_AS]->(l:Listing) "
        "WITH c, collect(l.ticker)[0] AS ticker, "
        "  CASE WHEN toLower(c.name) = toLower($q) THEN 3 "
        "       WHEN toLower(c.name) STARTS WITH toLower($q) THEN 2 "
        "       ELSE 0 END AS rank "
        "RETURN c.gmr_id AS id, c.name AS title, c.country AS country, "
        "  ticker, c.legal_form AS legal_form, c.city AS city, rank "
        "ORDER BY rank DESC, size(c.name) ASC, c.name ASC "
        "LIMIT $cap",
        **params,
    ).data()
    return [
        {
            "type": "company",
            "id": r["id"],
            "title": r["title"],
            "subtitle": r.get("ticker") or r.get("country") or "",
            "country": r.get("country"),
            "date": None,
            "score": r["rank"],
            "context": _ctx(r.get("city"), r.get("legal_form")),
            "meta": {"ticker": r.get("ticker")},
        }
        for r in rows
    ]


def _authorities(session, params, want_geo_filter):
    """Public bodies (contracting authorities) by name."""
    geo = ""
    if want_geo_filter and params.get("country"):
        geo = " AND toLower(a.country) = toLower($country)"
    name_expr = authority_name_expr("a", safe_lang(params.get("lang")))
    rows = session.run(
        "MATCH (a:Authority) "
        "WHERE toLower(a.name) CONTAINS toLower($q) "
        f"{geo} "
        "WITH a, CASE WHEN toLower(a.name) = toLower($q) THEN 3 "
        "             WHEN toLower(a.name) STARTS WITH toLower($q) THEN 2 "
        "             ELSE 0 END AS rank "
        "RETURN a.authority_id AS id, "
        f"  {name_expr} AS title, a.country AS country, "
        "  a.authority_type AS atype, rank "
        "ORDER BY rank DESC, size(a.name) ASC, a.name ASC "
        "LIMIT $cap",
        **params,
    ).data()
    return [
        {
            "type": "authority",
            "id": r["id"],
            "title": r["title"],
            "subtitle": r.get("country") or "",
            "country": r.get("country"),
            "date": None,
            "score": r["rank"],
            "context": _ctx(r.get("atype")),
            "meta": {"authority_type": r.get("atype")},
        }
        for r in rows
    ]


def _persons(session, params, want_geo_filter):  # pylint: disable=unused-argument
    """People (directors etc.) by full name."""
    rows = session.run(
        "MATCH (p:Person) "
        "WHERE toLower(coalesce(p.first_name,'') + ' ' + coalesce(p.name,'')) "
        "  CONTAINS toLower($q) "
        "OPTIONAL MATCH (p)-[:DIRECTS {current: true}]->(c:Company) "
        "WITH p, collect(DISTINCT c.name)[0..2] AS companies "
        "RETURN p.person_id AS id, "
        "  trim(coalesce(p.first_name,'') + ' ' + coalesce(p.name,'')) AS title, "
        "  p.birth_year AS birth_year, companies "
        "LIMIT $cap",
        **params,
    ).data()
    return [
        {
            "type": "person",
            "id": r["id"],
            "title": r["title"],
            "subtitle": ", ".join(r.get("companies") or []),
            "country": None,
            "date": None,
            "score": 0,
            "context": "",
            "meta": {"birth_year": r.get("birth_year")},
        }
        for r in rows
    ]


def _lobbyists(session, params, want_geo_filter):
    """EU transparency-register lobbyists (real props are ``detail_*``)."""
    geo = ""
    if want_geo_filter and params.get("country"):
        geo = " AND toLower(coalesce(l.detail_country,'')) = toLower($country)"
    date = _date_clause(
        "l.detail_registration_date",
        bool(params.get("date_from")), bool(params.get("date_to")),
    )
    rows = session.run(
        "MATCH (l:Lobbyist) "
        "WHERE (toLower(coalesce(l.detail_name, l.title, '')) CONTAINS toLower($q) "
        "   OR toLower(coalesce(l.detail_acronym,'')) CONTAINS toLower($q)) "
        f"{geo}{date} "
        "WITH l, coalesce(l.detail_name, l.title, '') AS nm "
        "RETURN l.disclosure_id AS id, nm AS title, "
        "  l.detail_acronym AS acronym, l.detail_country AS country, "
        "  l.detail_category AS category, "
        "  l.detail_goals AS goals, "
        "  l.detail_registration_date AS reg_date "
        "ORDER BY size(nm) ASC, nm ASC "
        "LIMIT $cap",
        **params,
    ).data()
    return [
        {
            "type": "lobbyist",
            "id": r["id"],
            "title": r["title"],
            "subtitle": r.get("acronym") or r.get("category") or "",
            "country": r.get("country"),
            "date": r.get("reg_date"),
            "score": 0,
            "context": _clip(r.get("goals")),
            "meta": {"category": r.get("category")},
        }
        for r in rows
    ]


def _contracts(session, params, want_geo_filter):
    """Procurement contracts by title; date on ``publication_date``.

    NUTS/geo filtering rides on the awarded company (contracts carry a buyer
    ``country`` but no materialized NUTS of their own).
    """
    geo = ""
    if want_geo_filter and params.get("country"):
        geo += " AND toLower(coalesce(ct.country,'')) = toLower($country)"
    if want_geo_filter and params.get("nuts"):
        geo += (
            " AND EXISTS { MATCH (ct)-[:AWARDED_TO]->(:Company)"
            "-[:LOCATED_IN]->(r:NUTSRegion) WHERE r.code STARTS WITH $nuts }"
        )
    date = _date_clause(
        "ct.publication_date",
        bool(params.get("date_from")), bool(params.get("date_to")),
    )
    rows = session.run(
        "MATCH (ct:Contract) "
        "WHERE ct.title IS NOT NULL AND toLower(ct.title) CONTAINS toLower($q) "
        f"{geo}{date} "
        "RETURN ct.ted_notice_id AS id, ct.title AS title, "
        "  ct.country AS country, ct.publication_date AS pub_date, "
        "  ct.value_eur AS value_eur "
        "ORDER BY ct.publication_date DESC "
        "LIMIT $cap",
        **params,
    ).data()
    return [
        {
            "type": "contract",
            "id": r["id"],
            "title": r["title"],
            "subtitle": r.get("country") or "",
            "country": r.get("country"),
            "date": r.get("pub_date"),
            "score": 0,
            "context": "",
            "meta": {"value_eur": r.get("value_eur")},
        }
        for r in rows
    ]


def _cohesion(session, params, want_geo_filter):
    """EU cohesion projects — ``:Disclosure {system:'eu-cohesion'}``."""
    geo = ""
    if want_geo_filter and params.get("country"):
        geo += " AND toLower(coalesce(d.detail_country,'')) = toLower($country)"
    if want_geo_filter and params.get("nuts"):
        geo += " AND toUpper(coalesce(d.detail_nuts_code,'')) STARTS WITH $nuts"
    date = _date_clause(
        "d.detail_start_date",
        bool(params.get("date_from")), bool(params.get("date_to")),
    )
    rows = session.run(
        "MATCH (d:Disclosure {system:'eu-cohesion'}) "
        "WHERE d.title IS NOT NULL AND toLower(d.title) CONTAINS toLower($q) "
        f"{geo}{date} "
        "RETURN d.disclosure_id AS id, d.title AS title, "
        "  d.detail_country AS country, d.detail_start_date AS start_date, "
        "  d.detail_fund AS fund, d.detail_nuts_code AS nuts_code, "
        "  d.detail_description AS description, d.detail_programme AS programme "
        "ORDER BY size(d.title) ASC "
        "LIMIT $cap",
        **params,
    ).data()
    return [
        {
            "type": "cohesion",
            "id": r["id"],
            "title": r["title"],
            "subtitle": r.get("fund") or r.get("country") or "",
            "country": r.get("country"),
            "date": r.get("start_date"),
            "score": 0,
            "context": _clip(r.get("description")) or _ctx(r.get("programme")),
            "meta": {"nuts_code": r.get("nuts_code"), "fund": r.get("fund")},
        }
        for r in rows
    ]


def _sanctions(session, params, want_geo_filter):  # pylint: disable=unused-argument
    """Sanctioned entities by name or alias; date on ``designation_date``."""
    date = _date_clause(
        "s.designation_date",
        bool(params.get("date_from")), bool(params.get("date_to")),
    )
    rows = session.run(
        "MATCH (s:SanctionedEntity) "
        "WHERE toLower(coalesce(s.name,'')) CONTAINS toLower($q) "
        "   OR any(a IN coalesce(s.aliases, []) "
        "          WHERE toLower(a) CONTAINS toLower($q)) "
        f"{date} "
        "WITH s, CASE WHEN toLower(coalesce(s.name,'')) = toLower($q) THEN 3 "
        "             WHEN toLower(coalesce(s.name,'')) STARTS WITH toLower($q) THEN 2 "
        "             ELSE 0 END AS rank "
        "RETURN s.entity_id AS id, s.name AS title, "
        "  s.sanction_regime AS regime, s.designation_date AS des_date, "
        "  s.legal_basis AS legal_basis, rank "
        "ORDER BY rank DESC, size(coalesce(s.name,'')) ASC "
        "LIMIT $cap",
        **params,
    ).data()
    return [
        {
            "type": "sanction",
            "id": r["id"],
            "title": r["title"],
            "subtitle": r.get("regime") or "",
            "country": None,
            "date": r.get("des_date"),
            "score": r["rank"],
            "context": _ctx(r.get("legal_basis")),
            "meta": {"regime": r.get("regime")},
        }
        for r in rows
    ]


# --- Legislation (CELLAR mirror in Virtuoso) --------------------------------

_MIRROR_GRAPH = "http://data.fontem.eu/graph/mirror/cellar/eu"

# ISO-639-1 → Publications Office language authority code, for picking the
# display title in the viewer's language.
_PO_LANG = {
    "bg": "BUL", "cs": "CES", "da": "DAN", "de": "DEU", "el": "ELL",
    "en": "ENG", "es": "SPA", "et": "EST", "fi": "FIN", "fr": "FRA",
    "ga": "GLE", "hr": "HRV", "hu": "HUN", "it": "ITA", "lt": "LIT",
    "lv": "LAV", "mt": "MLT", "nl": "NLD", "pl": "POL", "pt": "POR",
    "ro": "RON", "sk": "SLK", "sl": "SLV", "sv": "SWE",
}

# CELEX sector-3 document-type letter (position 5 in 3YYYYLNNNN).
_CELEX_SECTOR_3 = {
    "L": "Directive", "R": "Regulation", "D": "Decision",
    "H": "Recommendation", "A": "Opinion", "C": "Declaration",
    "E": "CFSP common position", "F": "JHA framework decision",
    "G": "Council resolution", "M": "Merger decision",
    "Q": "Institutional rules", "S": "ECSC act",
}

_CELEX_SECTORS = {
    "1": "Treaty", "2": "International agreement", "4": "Complementary act",
    "5": "Preparatory act", "6": "Case-law",
    "7": "National implementing measure", "9": "Parliamentary question",
}

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _celex_doc_type(celex: str) -> str:
    """Human label for a CELEX number's sector/type, best-effort."""
    if not celex:
        return "Legal document"
    if celex[0] == "3":
        return _CELEX_SECTOR_3.get(celex[5:6], "Legal act")
    return _CELEX_SECTORS.get(celex[0], "Legal document")


def _fallback_keywords(q: str) -> list[str]:
    """Naive keyword split for when the linguistics service is down.

    Keeps every token of length ≥ 2 plus all digit-bearing tokens —
    over-matching beats an empty results page.
    """
    return [t for t in _WORD_RE.findall(q.lower()) if len(t) >= 2 or t.isdigit()]


def _ft_pattern(keywords: list[str]) -> str:
    """AND-of-phrases pattern for Virtuoso ``bif:contains``.

    Tokens arrive word-only from the tokenizers; the strip of quote/backslash
    characters here is defence in depth, since the pattern is embedded in the
    SPARQL string.
    """
    safe = [re.sub(r"['\"\\]", "", kw) for kw in keywords]
    return " AND ".join(f'"{kw}"' for kw in safe if kw)


def _legislation_query(pattern: str, lang: str | None,
                       date_from: str | None, date_to: str | None,
                       cap: int) -> str:
    """SPARQL over the CELLAR mirror: works whose expression titles match.

    Groups by CELEX so a work counts once regardless of how many language
    versions matched; the display title prefers the viewer's language and
    falls back to any matched title.
    """
    po_lang = _PO_LANG.get(lang or "en", "ENG")
    if date_from or date_to:
        conds = []
        if date_from:
            conds.append(f'STR(?dd) >= "{date_from}"')
        if date_to:
            conds.append(f'STR(?dd) <= "{date_to}"')
        date_block = (
            "?w cdm:work_date_document ?dd . "
            f"FILTER({' && '.join(conds)}) BIND(STR(?dd) AS ?d)"
        )
    else:
        date_block = (
            "OPTIONAL { ?w cdm:work_date_document ?dd . BIND(STR(?dd) AS ?d) }"
        )
    return f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?celex (SAMPLE(?d) AS ?date) (MAX(?sc) AS ?score)
       (SAMPLE(?tpref) AS ?title_pref) (SAMPLE(?t) AS ?title_any)
WHERE {{
  GRAPH <{_MIRROR_GRAPH}> {{
    ?e cdm:expression_title ?t .
    ?t bif:contains '{pattern}' OPTION (score ?sc) .
    ?e cdm:expression_belongs_to_work ?w .
    ?w cdm:resource_legal_id_celex ?cx .
    BIND(STR(?cx) AS ?celex)
    {date_block}
    OPTIONAL {{
      ?ep cdm:expression_belongs_to_work ?w .
      ?ep cdm:expression_uses_language
        <http://publications.europa.eu/resource/authority/language/{po_lang}> .
      ?ep cdm:expression_title ?tpref .
    }}
  }}
}}
GROUP BY ?celex
ORDER BY DESC(MAX(?sc))
LIMIT {cap}
"""


def _legislation(virtuoso, linguistics, params):
    """EU legal acts from the CELLAR mirror by title keywords.

    Soft-fails to an empty list when Virtuoso is not configured, the
    query yields no keywords, or the store errors — search must degrade,
    not 500. Stop-word removal comes from the linguistics service with a
    naive local fallback.
    """
    if virtuoso is None:
        return []
    q = params["q"]
    lang = safe_lang(params.get("lang"))
    keywords = linguistics.keywords(q, lang) if linguistics else None
    if keywords is None:
        keywords = _fallback_keywords(q)
    pattern = _ft_pattern(keywords[:8])
    if not pattern:
        return []
    query = _legislation_query(
        pattern, lang, params.get("date_from"), params.get("date_to"),
        params["cap"],
    )
    try:
        rows = virtuoso.query(query)
    except SparqlTimeout:
        logger.warning("legislation search timed out (pattern=%s)", pattern)
        return []
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("legislation search failed (pattern=%s)", pattern)
        return []
    ui_lang = (lang or "en").upper()
    results = []
    for r in rows:
        celex = str(r.get("celex") or "")
        title = r.get("title_pref") or r.get("title_any") or celex
        results.append({
            "type": "legislation",
            "id": celex,
            "title": title,
            "subtitle": celex,
            "country": None,
            "date": r.get("date"),
            "score": 0,
            "context": _celex_doc_type(celex),
            "meta": {
                "celex": celex,
                "eurlex_url": (
                    "https://eur-lex.europa.eu/legal-content/"
                    f"{ui_lang}/TXT/?uri=CELEX:{celex}"
                ),
            },
        })
    return results


_HANDLERS = {
    "company": _companies,
    "authority": _authorities,
    "person": _persons,
    "lobbyist": _lobbyists,
    "contract": _contracts,
    "cohesion": _cohesion,
    "sanction": _sanctions,
}

# Which types a given filter constrains — a type NOT listed is left
# unfiltered by that filter (e.g. a date range doesn't drop companies,
# which have no relevant date, so they still appear). A country/NUTS geo
# filter, however, is a hard narrowing: a type with no geo dimension is
# excluded entirely when a geo filter is set.
_GEO_TYPES = {"company", "authority", "lobbyist", "contract", "cohesion"}
_DATE_TYPES = {"lobbyist", "contract", "cohesion", "sanction", "legislation"}


@router.get("/results")
@inject
def search_results(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    q: Annotated[str, Query(min_length=1, max_length=200)],
    types: Annotated[str | None, Query(max_length=200)] = None,
    country: Annotated[str | None, Query(max_length=3)] = None,
    nuts: Annotated[str | None, Query(max_length=8)] = None,
    date_from: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    date_to: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    lang: Annotated[str | None, Query(max_length=8)] = None,
    *,
    neo4j: FromDishka[Neo4jClient],
    virtuoso: FromDishka[VirtuosoClient | None],
    linguistics: FromDishka[LinguisticsClient | None],
) -> dict[str, Any]:
    """Faceted keyword search across the graph for the results page.

    Returns a flat, relevance-ranked page of typed results plus per-type
    counts (bounded by an over-fetch cap) for the facet sidebar. When a
    geo/date filter is set, only the types that carry that dimension are
    constrained; the others still match on the keyword.
    """
    selected = _parse_types(types)
    has_geo = bool(country or nuts)
    has_date = bool(date_from or date_to)
    cap = min(_MAX_PER_TYPE, offset + limit + 1)

    params = {
        "q": q, "country": country, "nuts": (nuts or "").upper() or None,
        "date_from": date_from, "date_to": date_to, "lang": lang, "cap": cap,
    }

    merged: list[dict] = []
    counts: dict[str, int] = {}
    with neo4j.session() as session:
        for t in selected:
            # A geo filter is a hard narrowing: a type with no geo dimension
            # can't satisfy it, so it's excluded. A date filter likewise
            # excludes types with no date property.
            if has_geo and t not in _GEO_TYPES:
                counts[t] = 0
                continue
            if has_date and t not in _DATE_TYPES:
                counts[t] = 0
                continue
            if t == "legislation":
                rows = _legislation(virtuoso, linguistics, params)
            else:
                rows = _HANDLERS[t](session, params, want_geo_filter=has_geo)
            counts[t] = len(rows)
            merged.extend(rows)

    # Rank across types: score desc, then the ALL_TYPES priority order,
    # then title for stability.
    prio = {t: i for i, t in enumerate(ALL_TYPES)}
    merged.sort(
        key=lambda r: (-r["score"], prio.get(r["type"], 99), (r["title"] or "")),
    )
    page = merged[offset:offset + limit]
    return {
        "query": q,
        "types": selected,
        "counts": counts,
        "total_shown": len(merged),
        "has_more": len(merged) > offset + limit,
        "results": page,
    }
