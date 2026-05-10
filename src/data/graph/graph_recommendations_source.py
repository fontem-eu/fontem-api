"""Country-scoped "top entities of interest" used by Public Spending
landing — the reader-side replacement for the old "search-only" home.

Two queries:
  - Top companies in the user's country, ranked by total contract
    value won. Country comes from `Company-[:LOCATED_IN]->NUTSRegion
    {level: 0}` (HQ-in-country), so the ranking matches "vendors
    headquartered here" rather than "vendors who sold to here".
  - Top authorities, ranked by total contract value awarded. Country
    is the alpha-3 stamped on `Authority.country` directly (the
    `LOCATED_IN` edge isn't populated for authorities — comment in
    graph_geo_source.py spells this out).

Both queries cap by `value_eur > 0` so we don't surface long-tail
entities with millions of zero-EUR contracts.
"""
from __future__ import annotations

from src.data.graph.neo4j_client import Neo4jClient
from src.services.location_service import LocationService

# `LocationService.alpha3_to_alpha2` returns the strict ISO alpha-2,
# which is "GB" for GBR. NUTS codes use "UK". Same override the
# adjacent graph_geo_source.py uses — keep them aligned by copying
# the constant rather than centralising it; the two callers can
# diverge later if NUTS adds more quirks.
_NUTS_ALPHA2_OVERRIDES = {"GB": "UK"}


class GraphRecommendationsSource:
    """Country-scoped top-N reads against the procurement graph."""

    def __init__(self, neo4j_client: Neo4jClient) -> None:
        self._neo4j = neo4j_client

    def top_companies_in_country(
        self, country_alpha3: str, limit: int = 10,
    ) -> list[dict]:
        """Companies HQ'd in the country, by total contract EUR won.

        `country_alpha3` is the canonical alpha-3 ISO code (PRT, DEU,
        FRA…). NUTSRegion uses alpha-2 codes (with EL/UK quirks), so
        we convert via LocationService — same mapping used at ETL time.
        """
        alpha2 = LocationService.alpha3_to_alpha2(country_alpha3)
        if not alpha2:
            return []
        # NUTS uses "EL" for Greece (LocationService already overrides
        # to that) and "UK" for the United Kingdom (it doesn't, so
        # patch here).
        alpha2 = _NUTS_ALPHA2_OVERRIDES.get(alpha2, alpha2)
        with self._neo4j.session() as session:
            rows = session.run(
                """
                MATCH (c:Company)-[:LOCATED_IN]->(:NUTSRegion {code: $alpha2, level: 0})
                MATCH (c)<-[:AWARDED_TO]-(ct:Contract)
                WITH c,
                     sum(toFloat(ct.value_eur)) AS total_value,
                     count(ct)                  AS contract_count
                WHERE total_value > 0
                RETURN c.gmr_id          AS id,
                       c.name            AS name,
                       total_value,
                       contract_count
                ORDER BY total_value DESC
                LIMIT $limit
                """,
                {"alpha2": alpha2, "limit": limit},
            ).data()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "total_value_eur": float(r["total_value"]),
                "contract_count": int(r["contract_count"]),
            }
            for r in rows
        ]

    def top_authorities_in_country(
        self, country_alpha3: str, limit: int = 10,
    ) -> list[dict]:
        """Authorities in the country, by total contract EUR awarded."""
        with self._neo4j.session() as session:
            rows = session.run(
                """
                MATCH (a:Authority {country: $country})-[:AWARDED]->(ct:Contract)
                WITH a,
                     sum(toFloat(ct.value_eur)) AS total_value,
                     count(ct)                  AS contract_count
                WHERE total_value > 0
                RETURN a.authority_id    AS id,
                       a.name            AS name,
                       total_value,
                       contract_count
                ORDER BY total_value DESC
                LIMIT $limit
                """,
                {"country": country_alpha3, "limit": limit},
            ).data()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "total_value_eur": float(r["total_value"]),
                "contract_count": int(r["contract_count"]),
            }
            for r in rows
        ]
