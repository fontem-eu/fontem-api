"""Public Spending landing endpoints — country detection +
country-scoped "of interest" lists.

Two endpoints, both anonymous-callable:

  GET /euro-tracker/me/country
    → { "country": "PRT" | null, "source": "geoip"|"unknown" }

  GET /euro-tracker/recommendations?country=PRT&limit=10
    → { "country": "PRT",
        "companies":   [...top 10 companies HQ'd in PRT...],
        "authorities": [...top 10 authorities in PRT...] }
"""
from __future__ import annotations

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query, Request

from src.data.graph.graph_recommendations_source import (
    GraphRecommendationsSource,
)
from src.services.ip_to_country import (
    IpToCountryService,
    client_ip_from_request,
)


router = APIRouter(prefix="/euro-tracker", tags=["euro-tracker"])


@router.get("/me/country")
@inject
def me_country(
    request: Request,
    *,
    svc: FromDishka[IpToCountryService],
) -> dict:
    """Best-effort IP → alpha-3 country code.

    `source: "geoip"` when the lookup succeeded, `source: "unknown"`
    when the GeoIP DB isn't available or the IP didn't resolve. The
    frontend treats `country: null` as "show a country picker".
    """
    headers = dict(request.headers.items())
    remote_addr = request.client.host if request.client else None
    ip = client_ip_from_request(headers, remote_addr)
    country = svc.lookup(ip) if svc.available else None
    return {
        "country": country,
        "source": "geoip" if country else "unknown",
        # Surface the reason in unavailable cases so the front-end
        # can log / dev-mode-display it. Doesn't leak the user's IP.
        "geoip_unavailable_reason": (
            None if svc.available else svc.unavailable_reason
        ),
    }


@router.get("/recommendations")
@inject
def recommendations(
    country: str = Query(
        ..., min_length=3, max_length=3,
        description="Alpha-3 country code (PRT, DEU, FRA, …).",
    ),
    limit: int = Query(10, ge=1, le=50),
    *,
    svc: FromDishka[GraphRecommendationsSource],
) -> dict:
    """Top-N companies HQ'd in `country` + top-N authorities in
    `country`, both ranked by total contract value EUR.

    The two queries run sequentially against the shared Neo4j
    driver; they're sub-second on the production graph (procurement
    data is well-indexed by country at NUTS-0).
    """
    country_a3 = country.upper()
    return {
        "country": country_a3,
        "companies":   svc.top_companies_in_country(country_a3, limit=limit),
        "authorities": svc.top_authorities_in_country(country_a3, limit=limit),
    }
