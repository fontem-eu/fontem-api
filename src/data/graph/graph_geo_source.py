"""
Graph Geo Source
================
GeoSource backed by Neo4j. Aggregates entities and contracts across the
NUTS hierarchy via LOCATED_IN and PART_OF edges.
"""
from __future__ import annotations

import logging

from ...analysis.geo_source import GeoSource
from .neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

# Traversal depth: NUTS 3 → parent NUTS 0 is at most 3 PART_OF hops.
_MAX_NUTS_DEPTH = 3

_METRICS = {"companies", "contracts", "contracts_eur"}
_ENTITY_METRICS = {"contracts", "contracts_eur"}

# Template for company-centric entity query.  Authorities awarding contracts
# to this company are the geographic anchor.
_COMPANY_QUERY = """
MATCH (c:Company {{gmr_id: $entity_id}})<-[:AWARDED_TO]-(ct:Contract)<-[:AWARDED]-(auth:Authority)
MATCH (auth)-[:LOCATED_IN]->(leaf:NUTSRegion)
MATCH (leaf)-[:PART_OF*0..{depth}]->(region:NUTSRegion {{level: $level}})
WHERE ($scope IS NULL OR region.code STARTS WITH $scope)
RETURN region.code AS code,
       region.name AS name,
       region.level AS level,
       {value_expr} AS value
ORDER BY value DESC
"""

# Template for authority-centric entity query.  Companies receiving contracts
# from this authority are the geographic anchor.
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
            value_expr = "count(DISTINCT ct)"
            entity_match = (
                "(e:Company)-[:LOCATED_IN]->(sub:NUTSRegion), "
                "(ct:Contract)-[:AWARDED_TO]->(e)"
            )
        else:  # contracts_eur
            value_expr = "coalesce(sum(toFloat(ct.value_eur)), 0)"
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
            "count(DISTINCT ct)"
            if metric == "contracts"
            else "coalesce(sum(toFloat(ct.value_eur)), 0.0)"
        )
        # Max PART_OF hops needed to reach any level from any leaf level.
        fmt = {"depth": _MAX_NUTS_DEPTH, "value_expr": value_expr}
        params = {
            "entity_id": entity_id,
            "level": level,
            "scope": scope_nuts,
        }
        with self._neo4j.session() as session:
            # Try company path first; fall back to authority path.
            rows = session.run(
                _COMPANY_QUERY.format(**fmt), **params
            ).data()
            if not rows:
                rows = session.run(
                    _AUTHORITY_QUERY.format(**fmt), **params
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
