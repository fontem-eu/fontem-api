"""Sitemap shards for graph entities: listed companies and authorities.

The story and core shards live in fontem-community-api, which owns that
content. These two need the graph, so they are served here and routed to
by nginx; the index over there lists them.

WHY ONE SHARD PER COUNTRY
-------------------------
A single global "top N authorities" list buries small member states. The
whole of Malta and Cyprus would sit below the German 500th, so a
50k-URL global cut would still publish nothing from either. Sharding by
country gives every member state its own budget, and it happens to make
each shard one bounded query — Germany, the largest, aggregates in under
a second, so nothing here needs precomputing or caching beyond the HTTP
cache header.

NOT ALL OF THIS IS LISTED YET
-----------------------------
The companies shard is wired into the index; the authorities shard is
not, and must not be until the frontend has an ``/authority/:id`` route.
It has none today — the SPA catch-all answers 200 with a not-found view,
so publishing 16,000 authority URLs would be publishing 16,000
soft-404s, which is worse for the site than publishing nothing. The
query and the endpoint are correct and ready; enabling them is one line
in the index once the page exists.

ALPHA-3 ONLY
------------
``country`` is alpha-3 everywhere in the graph by convention. Company
carried an alpha-2 drift from two loaders (fixed in #405); until the
backfill runs, roughly 2,300 European listed companies still hold
alpha-2 codes and will be absent from these shards. That is the correct
behaviour for a sitemap — publish what the convention can find, rather
than widening the query and hiding the migration.
"""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import os
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Response
from neo4j import READ_ACCESS

from src.data.graph.neo4j_client import Neo4jClient

router = APIRouter(tags=["sitemap"])

_CANONICAL_URL = os.environ.get("CANONICAL_URL", "https://fontem.eu")

#: EU-27 + EEA + CH + GB, alpha-3. The set the platform covers.
COUNTRIES: tuple[str, ...] = (
    "AUT", "BEL", "BGR", "HRV", "CYP", "CZE", "DNK", "EST", "FIN", "FRA",
    "DEU", "GRC", "HUN", "IRL", "ITA", "LVA", "LTU", "LUX", "MLT", "NLD",
    "POL", "PRT", "ROU", "SVK", "SVN", "ESP", "SWE", "ISL", "LIE", "NOR",
    "CHE", "GBR",
)

#: Per country, not globally — see the module docstring.
AUTHORITIES_PER_COUNTRY = 500

#: Contracts are read as a bounded aggregation; this is belt and braces
#: against a pathological plan, not a tuning knob.
_QUERY_TIMEOUT_S = 30.0

_TOP_AUTHORITIES = """
MATCH (a:Authority)-[:AWARDED]->(c:Contract)
WHERE a.country = $country AND c.is_current AND c.value_eur IS NOT NULL
WITH a, sum(c.value_eur) AS total
ORDER BY total DESC
LIMIT $limit
RETURN a.authority_id AS id
"""

#: Active listings only: a delisted shell is not something to invite a
#: crawler to index.
_LISTED_COMPANIES = """
MATCH (c:Company)-[:LISTED_AS]->(l:Listing)
WHERE l.active AND c.country = $country AND c.gmr_id IS NOT NULL
RETURN DISTINCT c.gmr_id AS id
"""


def _xml(body: str) -> Response:
    return Response(
        content=body,
        media_type="application/xml",
        # Entity rankings move on ETL cadence, not per request.
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _urlset(paths: list[str], changefreq: str) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    urls = "\n".join(
        f"  <url><loc>{escape(_CANONICAL_URL)}{escape(p)}</loc>"
        f"<lastmod>{today}</lastmod>"
        f"<changefreq>{changefreq}</changefreq></url>"
        for p in paths
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n</urlset>\n"
    )


def _country_or_404(country: str) -> str:
    code = country.upper()
    if code not in COUNTRIES:
        # Explicit 404 rather than an empty urlset: an unknown code is a
        # broken link in the index, and an empty file would hide that.
        raise HTTPException(status_code=404, detail=f"unknown country {country!r}")
    return code


def _read(neo4j: Neo4jClient, query: str, **params) -> list[str]:
    with neo4j.session(default_access_mode=READ_ACCESS) as session:
        with session.begin_transaction(timeout=_QUERY_TIMEOUT_S) as tx:
            return [r["id"] for r in tx.run(query, parameters=params) if r["id"]]


@router.get("/sitemap-authorities-{country}.xml", include_in_schema=False)
@inject
def sitemap_authorities(country: str, neo4j: FromDishka[Neo4jClient]) -> Response:
    code = _country_or_404(country)
    ids = _read(neo4j, _TOP_AUTHORITIES, country=code, limit=AUTHORITIES_PER_COUNTRY)
    return _xml(_urlset([f"/authority/{i}" for i in ids], "monthly"))


@router.get("/sitemap-companies-{country}.xml", include_in_schema=False)
@inject
def sitemap_companies(country: str, neo4j: FromDishka[Neo4jClient]) -> Response:
    code = _country_or_404(country)
    ids = _read(neo4j, _LISTED_COMPANIES, country=code)
    return _xml(_urlset([f"/company/{i}" for i in ids], "weekly"))
