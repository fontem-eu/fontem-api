"""
Contracts API Router
=====================
Endpoints for procurement data — company contracts, authority contracts,
contract detail, sector summary, and unified search.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..dependencies import get_contract_source, get_data_source, get_person_source

router = APIRouter(tags=["contracts"])


@router.get("/companies/{gmr_id}/contracts")
def company_contracts(
    gmr_id: str,
    years: int = Query(5, ge=1, le=20),
    limit: int = Query(50, ge=1, le=200),
    source=Depends(get_contract_source),
):
    """Contracts awarded to a company."""
    return source.get_company_contracts(gmr_id, years=years, limit=limit)


@router.get("/companies/{gmr_id}")
def company_profile(
    gmr_id: str,
    source=Depends(get_contract_source),
    person_source=Depends(get_person_source),
):
    """Company profile with procurement summary, directors, and group."""
    contracts = source.get_company_contracts(gmr_id, years=5, limit=5)
    directors = person_source.get_company_directors(gmr_id)

    # Corporate group (via SUBSIDIARY_OF)
    group = None
    with source._neo4j.session() as session:  # pylint: disable=protected-access
        group_data = session.run(
            "MATCH (member:Company {gmr_id: $gid}) "
            "OPTIONAL MATCH (member)-[:SUBSIDIARY_OF*1..5]->(ancestor) "
            "WHERE NOT (ancestor)-[:SUBSIDIARY_OF]->() "
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
def authority_contracts(
    authority_id: str,
    years: int = Query(5, ge=1, le=20),
    limit: int = Query(50, ge=1, le=200),
    source=Depends(get_contract_source),
):
    """Contracts issued by an authority."""
    return source.get_authority_contracts(
        authority_id, years=years, limit=limit,
    )


@router.get("/authorities/{authority_id}")
def authority_profile(
    authority_id: str,
    source=Depends(get_contract_source),
):
    """Authority profile with spending summary."""
    contracts = source.get_authority_contracts(
        authority_id, years=5, limit=5,
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
def sector_summary(
    country: str | None = Query(None),
    year: int | None = Query(None),
    source=Depends(get_contract_source),
):
    """Aggregated contract values by CPV sector."""
    return source.get_sector_summary(country=country, year=year)


@router.get("/contracts/{notice_id}")
def contract_detail(
    notice_id: str,
    source=Depends(get_contract_source),
):
    """Full detail for a single contract."""
    result = source.get_contract_detail(notice_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    return result


@router.get("/search")
def unified_search(  # pylint: disable=too-many-locals
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    contract_source=Depends(get_contract_source),
):
    """Unified search across companies and authorities.

    Prioritizes: listed companies > companies with contracts > all others.
    Matches on both company name AND ticker symbol.
    Deduplicates by gmr_id (a company with multiple listings appears once).
    """
    with contract_source._neo4j.session() as session:  # pylint: disable=protected-access
        # 1. Listed companies matching by ticker OR name (highest priority)
        listed = session.run(
            "MATCH (c:Company)-[:LISTED_AS]->(l:Listing) "
            "WHERE toLower(l.ticker) STARTS WITH toLower($q) "
            "   OR toLower(c.name) CONTAINS toLower($q) "
            "RETURN DISTINCT c.gmr_id AS gmr_id, c.name AS name, "
            "  c.country AS country, "
            "  collect(l.ticker)[0] AS ticker, "
            "  collect(l.exchange)[0] AS exchange, "
            "  collect(l.currency)[0] AS currency, "
            "  true AS is_active "
            "LIMIT $limit",
            q=q, limit=limit,
        ).data()

        seen = {r["gmr_id"] for r in listed}

        # 2. Companies with contracts (procurement-only, no listing)
        remaining = max(0, limit - len(listed))
        procurement = []
        if remaining > 0:
            procurement = session.run(
                "MATCH (ct:Contract)-[:AWARDED_TO]->(c:Company) "
                "WHERE NOT c.gmr_id IN $seen "
                "  AND toLower(c.name) CONTAINS toLower($q) "
                "RETURN DISTINCT c.gmr_id AS gmr_id, c.name AS name, "
                "  c.country AS country, "
                "  null AS ticker, null AS exchange, null AS currency, "
                "  null AS is_active "
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

        # 3. Authorities
        auth_rows = session.run(
            "MATCH (a:Authority) "
            "WHERE toLower(a.name) CONTAINS toLower($q) "
            "RETURN a.authority_id AS authority_id, "
            "  a.name AS name, a.country AS country "
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

    return {
        "query": q,
        "companies": company_rows,
        "authorities": auth_rows,
        "persons": person_rows,
    }
