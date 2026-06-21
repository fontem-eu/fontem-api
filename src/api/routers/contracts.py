"""
Contracts API Router
=====================
Endpoints for procurement data — company contracts, authority contracts,
contract detail, sector summary, and unified search.
"""
from __future__ import annotations

import httpx
from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from src.analysis.contract_data_source import ContractDataSource
from src.analysis.person_data_source import PersonDataSource
from src.api.lang import authority_name_expr, safe_lang
from src.data.graph.neo4j_client import Neo4jClient
from src.services.ted_lookup import (
    TedLookupError,
    detail_url_for,
    resolve_publication_number,
)


router = APIRouter(tags=["contracts"])


@router.get("/companies/{gmr_id}/contracts")
@inject
def company_contracts(
    gmr_id: str,
    years: int = Query(5, ge=1, le=20),
    limit: int = Query(50, ge=1, le=200),
    lang: str | None = Query(None),
    *,
    source: FromDishka[ContractDataSource],
):
    """Contracts awarded to a company. `lang` (ISO-639-1) swaps in the
    translated Authority name when available, falls back to the stored
    original."""
    return source.get_company_contracts(
        gmr_id, years=years, limit=limit, lang=safe_lang(lang),
    )


@router.get("/companies/{gmr_id}")
@inject
def company_profile(
    gmr_id: str,
    lang: str | None = Query(None),
    *,
    source: FromDishka[ContractDataSource],
    person_source: FromDishka[PersonDataSource],
    neo4j: FromDishka[Neo4jClient],
):
    """Company profile with procurement summary, directors, and group."""
    contracts = source.get_company_contracts(
        gmr_id, years=5, limit=5, lang=safe_lang(lang),
    )
    directors = person_source.get_company_directors(gmr_id)

    # Corporate group (via SUBSIDIARY_OF)
    group = None
    with neo4j.session() as session:
        group_data = session.run(
            "MATCH (member:Company {gmr_id: $gid}) "
            "OPTIONAL MATCH (member)-[:SUBSIDIARY_OF*1..5]->(ancestor) "
            "WHERE NOT EXISTS { (ancestor)-[:SUBSIDIARY_OF]->() } "
            "WITH COALESCE(ancestor, member) AS root "
            "MATCH (root)<-[:SUBSIDIARY_OF*0..5]-(child) "
            "OPTIONAL MATCH (ct:Contract)-[:AWARDED_TO]->(child) "
            "RETURN root.gmr_id AS root_id, root.name AS root_name, "
            "  root.country AS root_country, "
            "  child.gmr_id AS child_id, child.name AS child_name, "
            "  child.country AS child_country, "
            "  count(ct) AS contracts "
            "ORDER BY contracts DESC",
            gid=gmr_id,
        ).data()
        if group_data and len(group_data) > 1:
            group = {
                "root_name": group_data[0]["root_name"],
                "root_country": group_data[0]["root_country"],
                "root_id": group_data[0]["root_id"],
                "entity_count": len(group_data),
                "members": [
                    {
                        "gmr_id": r["child_id"],
                        "name": r["child_name"],
                        "country": r["child_country"],
                        "contracts": r["contracts"],
                    }
                    for r in group_data
                ],
            }

    return {
        "gmr_id": gmr_id,
        "company_name": contracts.get("company_name"),
        "country": contracts.get("country"),
        "contract_count": contracts.get("contract_count", 0),
        "total_contract_value_eur": contracts.get(
            "total_contract_value_eur", 0
        ),
        "recent_contracts": contracts.get("contracts", [])[:5],
        "directors": directors,
        "group": group,
    }


@router.get("/authorities/{authority_id}/contracts")
@inject
def authority_contracts(
    authority_id: str,
    years: int = Query(5, ge=1, le=20),
    limit: int = Query(50, ge=1, le=200),
    lang: str | None = Query(None),
    *,
    source: FromDishka[ContractDataSource],
):
    """Contracts issued by an authority."""
    return source.get_authority_contracts(
        authority_id, years=years, limit=limit, lang=safe_lang(lang),
    )


@router.get("/authorities/{authority_id}")
@inject
def authority_profile(
    authority_id: str,
    lang: str | None = Query(None),
    *,
    source: FromDishka[ContractDataSource],
):
    """Authority profile with spending summary."""
    contracts = source.get_authority_contracts(
        authority_id, years=5, limit=5, lang=safe_lang(lang),
    )
    return {
        "authority_id": authority_id,
        "authority_name": contracts.get("authority_name"),
        "country": contracts.get("country"),
        "contract_count": contracts.get("contract_count", 0),
        "total_spend_eur": contracts.get("total_spend_eur", 0),
        "recent_contracts": contracts.get("contracts", [])[:5],
    }


@router.get("/contracts/sectors")
@inject
def sector_summary(
    country: str | None = Query(None),
    year: int | None = Query(None),
    *,
    source: FromDishka[ContractDataSource],
):
    """Aggregated contract values by CPV sector."""
    return source.get_sector_summary(country=country, year=year)


@router.get("/contracts/single-bidder-rate")
@inject
def single_bidder_rate(
    country: str | None = Query(None),
    cpv: str | None = Query(None),
    *,
    source: FromDishka[ContractDataSource],
):
    """Single-bidder rate (EC Single Market Scoreboard headline) over
    contracts with a known bidder count, optionally scoped by authority
    country and/or CPV prefix."""
    return source.get_single_bidder_stats(country=country, cpv=cpv)


@router.get("/contracts/single-bidder-by-country")
@inject
def single_bidder_by_country(
    min_sample: int = Query(20, ge=1),
    limit: int = Query(40, ge=1, le=200),
    *,
    source: FromDishka[ContractDataSource],
):
    """Single-bidder rate per authority country (>= min_sample contracts),
    highest first — the cross-country benchmark."""
    return source.get_single_bidder_by_country(min_sample=min_sample, limit=limit)


@router.get("/contracts/{notice_id}")
@inject
def contract_detail(
    notice_id: str,
    lang: str | None = Query(None),
    *,
    source: FromDishka[ContractDataSource],
):
    """Full detail for a single contract."""
    result = source.get_contract_detail(notice_id, lang=safe_lang(lang))
    if result is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    return result


@router.get(
    "/contracts/{notice_id}/ted-link",
    responses={
        302: {"description": "redirect to the canonical TED detail URL"},
        404: {"description": "TED has no published notice for this UUID"},
        502: {"description": "TED search API unavailable or errored"},
    },
)
@inject
def contract_ted_link(
    notice_id: str,
    source: FromDishka[ContractDataSource],
) -> RedirectResponse:
    """302 to the canonical TED notice detail page for ``notice_id``.

    Two paths to a publication-number:

    1. **Stored on the Contract row** — the TED ETL resolves
       publication-numbers via TED's v3 search API at ingest time and
       persists them as ``ted_publication_number`` on the Contract
       node. When present, the redirect is O(1): one Neo4j read, no
       TED traffic, cold-pod-friendly.
    2. **Looked up live** — for contracts ingested before the ETL
       captured the field, or whose publication-number wasn't yet
       assigned at ingest (queued / not-yet-published), fall back to
       the live TED v3 search call. LRU-cached process-locally so
       repeat clicks are still O(1).

    Errors:
    - ``404`` — TED has no record of the UUID (notice never published
      or never existed). Surfaced from the lookup so the message
      doesn't drift between the ETL and the runtime path.
    - ``502`` — TED search transport error (DNS, timeout, 5xx).
      Distinct from 404 so clients can retry the rare "TED is down"
      case without retrying the permanent "no such notice" case.
    """
    stored = source.get_stored_publication_number(notice_id)
    if stored:
        return RedirectResponse(url=detail_url_for(stored), status_code=302)
    try:
        pub_num = resolve_publication_number(notice_id)
    except TedLookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"TED search API error: {exc}",
        ) from exc
    return RedirectResponse(url=detail_url_for(pub_num), status_code=302)


@router.get("/search")
@inject
# contract_source is injected to pin the dependency in the OpenAPI surface
# and keep the dishka container wired the same as the other contract endpoints,
# even though this handler reads directly from Neo4j.
def unified_search(  # pylint: disable=too-many-locals,unused-argument
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    lang: str | None = Query(None),
    *,
    contract_source: FromDishka[ContractDataSource],
    neo4j: FromDishka[Neo4jClient],
):
    """Unified search across companies and authorities.

    Prioritizes: listed companies > companies with contracts > all others.
    Matches on both company name AND ticker symbol.
    Deduplicates by gmr_id (a company with multiple listings appears once).
    """
    with neo4j.session() as session:
        # 1. Listed companies matching by ticker OR name. Without a rank,
        # a CONTAINS-only hit ("Pineapple" contains "apple") can outrank
        # an exact-name match ("Apple Inc.") because Neo4j has no implicit
        # ordering — smoke test SEARCH-04 caught this when PINEAPPL.L
        # surfaced before AAPL for q="Apple". Score tiers:
        #   4 → name equals q exactly
        #   3 → ticker equals q exactly
        #   2 → name starts with q (Apple Inc., Apple Hospitality, ...)
        #   1 → ticker starts with q
        #   0 → name contains q (the fallback that PINEAPPLE POWER hits)
        # Within a tier, shorter names win (Apple Inc. → 10 chars beats
        # Apple Hospitality REIT, Inc. → 28 chars); alphabetical tiebreak
        # after that for determinism across reruns.
        listed = session.run(
            "MATCH (c:Company)-[:LISTED_AS]->(l:Listing) "
            "WHERE toLower(l.ticker) STARTS WITH toLower($q) "
            "   OR toLower(c.name) CONTAINS toLower($q) "
            "WITH c, l, "
            "  CASE "
            "    WHEN toLower(c.name) = toLower($q) THEN 4 "
            "    WHEN toLower(l.ticker) = toLower($q) THEN 3 "
            "    WHEN toLower(c.name) STARTS WITH toLower($q) THEN 2 "
            "    WHEN toLower(l.ticker) STARTS WITH toLower($q) THEN 1 "
            "    ELSE 0 END AS rank "
            "WITH c, max(rank) AS rank, "
            "  collect(l.ticker)[0] AS ticker, "
            "  collect(l.exchange)[0] AS exchange, "
            "  collect(l.currency)[0] AS currency "
            "RETURN c.gmr_id AS gmr_id, c.name AS name, "
            "  c.country AS country, "
            "  ticker, exchange, currency, "
            "  true AS is_active, rank "
            "ORDER BY rank DESC, size(c.name) ASC, c.name ASC "
            "LIMIT $limit",
            q=q, limit=limit,
        ).data()

        seen = {r["gmr_id"] for r in listed}

        # 2. Companies with contracts (procurement-only, no listing).
        # Same tier scheme as above; no ticker tier because there's no
        # Listing edge on these.
        remaining = max(0, limit - len(listed))
        procurement = []
        if remaining > 0:
            procurement = session.run(
                "MATCH (ct:Contract)-[:AWARDED_TO]->(c:Company) "
                "WHERE NOT c.gmr_id IN $seen "
                "  AND toLower(c.name) CONTAINS toLower($q) "
                "WITH DISTINCT c, "
                "  CASE "
                "    WHEN toLower(c.name) = toLower($q) THEN 4 "
                "    WHEN toLower(c.name) STARTS WITH toLower($q) THEN 2 "
                "    ELSE 0 END AS rank "
                "RETURN c.gmr_id AS gmr_id, c.name AS name, "
                "  c.country AS country, "
                "  null AS ticker, null AS exchange, null AS currency, "
                "  null AS is_active, rank "
                "ORDER BY rank DESC, size(c.name) ASC, c.name ASC "
                "LIMIT $remaining",
                q=q, seen=list(seen), remaining=remaining,
            ).data()

        company_rows = listed + procurement
        for r in company_rows:
            r["symbol"] = r.get("ticker")
            r["search_name"] = (
                f"{r.get('name', '')} {r.get('ticker', '')}".lower()
            )
            r["search_keywords"] = r["search_name"]
            r["data_source"] = (
                "esef" if r.get("currency") not in (None, "USD")
                else "edgar"
            )
            # `rank` is only used to sort inside the Cypher; clients see
            # the ordered list, not the raw score.
            r.pop("rank", None)

        # 3. Authorities — name coalesces with translation if lang given.
        # Search still matches on the original `a.name` field so results
        # surface regardless of which language the user ultimately views
        # them in (e.g. typing "ministero" still finds the Italian node
        # even when the viewer's locale is German).
        auth_name_expr = authority_name_expr("a", safe_lang(lang))
        auth_rows = session.run(
            "MATCH (a:Authority) "
            "WHERE toLower(a.name) CONTAINS toLower($q) "
            "RETURN a.authority_id AS authority_id, "
            f"  {auth_name_expr} AS name, a.country AS country "
            "LIMIT $limit",
            q=q, limit=limit,
        ).data()

        # 4. Persons
        person_rows = session.run(
            "MATCH (p:Person) "
            "WHERE toLower(p.first_name + ' ' + p.name) "
            "  CONTAINS toLower($q) "
            "OPTIONAL MATCH (p)-[r:DIRECTS {current: true}]->"
            "  (c:Company) "
            "RETURN DISTINCT p.person_id AS person_id, "
            "  p.first_name AS first_name, p.name AS name, "
            "  p.birth_year AS birth_year, "
            "  collect(DISTINCT c.name)[0..2] AS companies "
            "LIMIT $limit",
            q=q, limit=limit,
        ).data()

        # 5. Lobbyists
        lobbyist_rows = session.run(
            "MATCH (l:Lobbyist) "
            "WHERE toLower(l.name) CONTAINS toLower($q) "
            "   OR toLower(l.acronym) CONTAINS toLower($q) "
            "RETURN l.tr_id AS tr_id, l.name AS name, "
            "  l.acronym AS acronym, l.country AS country, "
            "  l.category AS category, l.ep_passes AS ep_passes, "
            "  l.cost_max AS cost_max "
            "LIMIT $limit",
            q=q, limit=limit,
        ).data()

    return {
        "query": q,
        "companies": company_rows,
        "authorities": auth_rows,
        "persons": person_rows,
        "lobbyists": lobbyist_rows,
    }
