"""
Contracts API Router
=====================
Endpoints for procurement data — company contracts, authority contracts,
contract detail, sector summary, and unified search.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..dependencies import get_contract_source, get_data_source

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
    financial=Depends(get_data_source),
):
    """Company profile with procurement summary."""
    contracts = source.get_company_contracts(gmr_id, years=5, limit=5)
    return {
        "gmr_id": gmr_id,
        "company_name": contracts.get("company_name"),
        "country": contracts.get("country"),
        "contract_count": contracts.get("contract_count", 0),
        "total_contract_value_eur": contracts.get(
            "total_contract_value_eur", 0
        ),
        "recent_contracts": contracts.get("contracts", [])[:5],
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
def unified_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    data_source=Depends(get_data_source),
    contract_source=Depends(get_contract_source),
):
    """Unified search across companies and authorities."""
    # Companies (from existing FinancialDataSource search)
    companies = data_source.search_tickers(q, limit=limit)

    # Authorities (from Neo4j)
    with contract_source._neo4j.session() as session:  # pylint: disable=protected-access
        auth_rows = session.run(
            "MATCH (a:Authority) "
            "WHERE toLower(a.name) CONTAINS toLower($q) "
            "RETURN a.authority_id AS authority_id, "
            "  a.name AS name, a.country AS country "
            "LIMIT $limit",
            q=q, limit=limit,
        ).data()

    return {
        "query": q,
        "companies": companies,
        "authorities": auth_rows,
    }
