"""
Graph Geo Source
================
GeoSource backed by Neo4j. Aggregates entities and contracts across the
NUTS hierarchy via LOCATED_IN and PART_OF edges.
"""
from __future__ import annotations

import logging

from ...analysis.geo_source import GeoSource
from ...services.location_service import LocationService
from ._value_quality import canonical_predicate, trusted_value_sum
from .neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

# Traversal depth: NUTS 3 → parent NUTS 0 is at most 3 PART_OF hops.
_MAX_NUTS_DEPTH = 3

_METRICS = {"companies", "contracts", "contracts_eur"}
_ENTITY_METRICS = {"contracts", "contracts_eur"}

# Authorities have no LOCATED_IN edges (authorities_linked=0 in production).
# Instead we aggregate contracts by authority.country (alpha-3) and map to
# NUTS level-0 codes using LocationService.  For level > 0 the company
# receives no sub-national authority geography, so the result is empty.
_COMPANY_COUNTRY_QUERY = """
MATCH (c:Company {{gmr_id: $entity_id}})<-[:AWARDED_TO]-(ct:Contract)<-[:AWARDED]-(auth:Authority)
WHERE auth.country IS NOT NULL
  AND ($scope_a3 IS NULL OR auth.country = $scope_a3)
RETURN auth.country AS country_a3,
       {value_expr} AS value
ORDER BY value DESC
"""

# Template for authority-centric entity query.  Companies receiving contracts
# from this authority are the geographic anchor; LOCATED_IN is available on
# Company nodes so all NUTS levels work here.
_AUTHORITY_QUERY = """
MATCH (auth:Authority {{authority_id: $entity_id}})-[:AWARDED]->(ct:Contract)-[:AWARDED_TO]->(c:Company)
MATCH (c)-[:LOCATED_IN]->(leaf:NUTSRegion)
MATCH (leaf)-[:PART_OF*0..{depth}]->(region:NUTSRegion {{level: $level}})
WHERE ($scope IS NULL OR region.code STARTS WITH $scope)
RETURN region.code AS code,
       region.name AS name,
       region.level AS level,
       {value_expr} AS value
ORDER BY value DESC
"""

# GBR → alpha-2 "GB" via pycountry, but NUTS uses "UK".
_NUTS_ALPHA2_OVERRIDES = {"GB": "UK"}


def _alpha3_to_nuts_code(alpha3: str) -> str | None:
    """Convert an alpha-3 country code to its NUTS level-0 code."""
    a2 = LocationService.alpha3_to_alpha2(alpha3)
    if a2 is None:
        return None
    return _NUTS_ALPHA2_OVERRIDES.get(a2, a2)


class GraphGeoSource(GeoSource):
    """Production geo source backed by Neo4j."""

    def __init__(self, neo4j_client: Neo4jClient) -> None:
        self._neo4j = neo4j_client

    def aggregate_by_nuts(
        self,
        level: int,
        metric: str,
        scope_nuts: str | None = None,
        connected_to_country: str | None = None,
    ) -> list[dict]:
        if level not in range(_MAX_NUTS_DEPTH + 1):
            raise ValueError(f"level must be 0..{_MAX_NUTS_DEPTH}, got {level}")
        if metric not in _METRICS:
            raise ValueError(f"metric must be one of {_METRICS}, got {metric}")
        if level == 3 and not scope_nuts:
            raise ValueError("level=3 requires scope_nuts (a NUTS 1 ancestor)")

        # Entity subquery: only nodes matching the connected-to filter pass.
        # If no filter, match any Company/Authority.
        if connected_to_country:
            entity_filter = (
                "EXISTS { "
                "  MATCH (e)-[*1..3]-(other) "
                "  WHERE (other:Company OR other:Authority) "
                "    AND other.country = $connected_to "
                "}"
            )
        else:
            entity_filter = "true"

        # Scope filter: restrict to descendants of the given ancestor.
        # PART_OF chains from child → parent, so we traverse *upward* from the
        # region to check ancestry.
        scope_filter = (
            "EXISTS { MATCH (region)-[:PART_OF*0..3]->(a:NUTSRegion {code: $scope}) }"
            if scope_nuts else "true"
        )

        if metric == "companies":
            value_expr = "count(DISTINCT e)"
            entity_match = "(e:Company)-[:LOCATED_IN]->(sub:NUTSRegion)"
        elif metric == "contracts":
            value_expr = f"count(DISTINCT CASE WHEN {canonical_predicate('ct')} THEN ct END)"
            entity_match = (
                "(e:Company)-[:LOCATED_IN]->(sub:NUTSRegion), "
                "(ct:Contract)-[:AWARDED_TO]->(e)"
            )
        else:  # contracts_eur
            value_expr = f"coalesce({trusted_value_sum('ct', cast=True)}, 0)"
            entity_match = (
                "(e:Company)-[:LOCATED_IN]->(sub:NUTSRegion), "
                "(ct:Contract)-[:AWARDED_TO]->(e)"
            )

        # Roll child regions up to the requested level via PART_OF*.
        # At level 3 we need distance 0; level 0 is distance 3 from NUTS 3.
        query = f"""
        MATCH (region:NUTSRegion {{level: $level}})
        WHERE {scope_filter}
        OPTIONAL MATCH (sub:NUTSRegion)-[:PART_OF*0..{_MAX_NUTS_DEPTH}]->(region)
        OPTIONAL MATCH {entity_match}
        WHERE {entity_filter}
        RETURN region.code AS code,
               region.name AS name,
               region.level AS level,
               {value_expr} AS value
        ORDER BY value DESC
        """
        params = {
            "level": level,
            "scope": scope_nuts,
            "connected_to": connected_to_country,
        }
        with self._neo4j.session() as session:
            rows = session.run(query, **params).data()
        return [
            {
                "nuts_code": r["code"],
                "label": r["name"],
                "level": r["level"],
                "value": r["value"],
            }
            for r in rows
        ]

    def aggregate_entity_by_nuts(
        self,
        entity_id: str,
        level: int,
        metric: str,
        scope_nuts: str | None = None,
    ) -> list[dict]:
        if level not in range(_MAX_NUTS_DEPTH + 1):
            raise ValueError(f"level must be 0..{_MAX_NUTS_DEPTH}, got {level}")
        if metric not in _ENTITY_METRICS:
            raise ValueError(
                f"entity metric must be one of {_ENTITY_METRICS}, got {metric}"
            )

        value_expr = (
            f"count(DISTINCT CASE WHEN {canonical_predicate('ct')} THEN ct END)"
            if metric == "contracts"
            else f"coalesce({trusted_value_sum('ct', cast=True)}, 0.0)"
        )
        fmt = {"depth": _MAX_NUTS_DEPTH, "value_expr": value_expr}

        with self._neo4j.session() as session:
            # ── Company path: aggregate by authority country ──────────────────
            # Authorities have no LOCATED_IN edges, so we use auth.country
            # (alpha-3) for level-0 aggregation.  For level > 0 within a scope,
            # convert the scope NUTS prefix to the corresponding alpha-3 country.
            scope_a3: str | None = None
            if scope_nuts and level > 0:
                # scope_nuts starts with the 2-char NUTS country code.
                nuts_country = scope_nuts[:2].upper()
                scope_a3 = LocationService.alpha2_to_alpha3(nuts_country)

            country_rows = session.run(
                _COMPANY_COUNTRY_QUERY.format(value_expr=value_expr),
                entity_id=entity_id,
                scope_a3=scope_a3,
            ).data()

            if country_rows:
                # Map alpha-3 → NUTS code and fetch region names.
                nuts_codes = [
                    _alpha3_to_nuts_code(r["country_a3"])
                    for r in country_rows
                ]
                value_by_nuts = {
                    _alpha3_to_nuts_code(r["country_a3"]): r["value"]
                    for r in country_rows
                    if _alpha3_to_nuts_code(r["country_a3"])
                }

                if level == 0:
                    # Return one row per country NUTS code.
                    region_rows = session.run(
                        "MATCH (r:NUTSRegion {level: 0}) "
                        "WHERE r.code IN $codes "
                        "RETURN r.code AS code, r.name AS name, r.level AS level",
                        codes=[c for c in nuts_codes if c],
                    ).data()
                    return [
                        {
                            "nuts_code": r["code"],
                            "label": r["name"],
                            "level": r["level"],
                            "value": value_by_nuts.get(r["code"], 0),
                        }
                        for r in region_rows
                    ]

                # For level > 0 we only know authority country, not sub-region.
                # Return empty — the map will show a no-data state for this entity.
                logger.debug(
                    "entity %s: no sub-national authority geography available "
                    "for level %d (authorities_linked=0); returning empty",
                    entity_id, level,
                )
                return []

            # ── Authority path: use company LOCATED_IN ────────────────────────
            rows = session.run(
                _AUTHORITY_QUERY.format(**fmt),
                entity_id=entity_id,
                level=level,
                scope=scope_nuts,
            ).data()

        return [
            {
                "nuts_code": r["code"],
                "label": r["name"],
                "level": r["level"],
                "value": r["value"],
            }
            for r in rows
        ]
