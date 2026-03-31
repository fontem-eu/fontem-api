"""Graph Person Source — Neo4j-backed person/director queries."""
from __future__ import annotations

import logging

from ...analysis.person_data_source import PersonDataSource
from .neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)


class GraphPersonSource(PersonDataSource):
    """Production person data source backed by Neo4j."""

    def __init__(self, neo4j_client: Neo4jClient) -> None:
        self._neo4j = neo4j_client

    def get_company_directors(
        self, gmr_id: str, include_former: bool = False,
    ) -> list[dict]:
        """Return directors/officers for a company."""
        where = "" if include_former else "AND r.current = true "
        with self._neo4j.session() as session:
            rows = session.run(
                "MATCH (p:Person)-[r:DIRECTS]->(c:Company {gmr_id: $gid}) "
                f"WHERE true {where}"
                "RETURN p.person_id AS person_id, "
                "  p.name AS name, p.first_name AS first_name, "
                "  p.birth_year AS birth_year, "
                "  p.nationality AS nationality, "
                "  r.role AS role, r.start_date AS start_date, "
                "  r.end_date AS end_date, r.current AS current "
                "ORDER BY r.current DESC, r.start_date DESC",
                gid=gmr_id,
            ).data()
        return rows

    def get_person_roles(self, person_id: str) -> list[dict]:
        """Return all company roles held by a person."""
        with self._neo4j.session() as session:
            rows = session.run(
                "MATCH (p:Person {person_id: $pid})-[r:DIRECTS]->(c:Company) "
                "OPTIONAL MATCH (c)-[:LISTED_AS]->(l:Listing) "
                "RETURN c.gmr_id AS gmr_id, c.name AS company_name, "
                "  c.country AS country, l.ticker AS ticker, "
                "  r.role AS role, r.start_date AS start_date, "
                "  r.end_date AS end_date, r.current AS current "
                "ORDER BY r.current DESC, r.start_date DESC",
                pid=person_id,
            ).data()
        return rows

    def search_persons(
        self, name: str, limit: int = 10,
    ) -> list[dict]:
        """Search persons by name."""
        with self._neo4j.session() as session:
            rows = session.run(
                "MATCH (p:Person) "
                "WHERE toLower(p.name) CONTAINS toLower($q) "
                "   OR toLower(p.first_name + ' ' + p.name) "
                "      CONTAINS toLower($q) "
                "OPTIONAL MATCH (p)-[r:DIRECTS {current: true}]->"
                "  (c:Company) "
                "RETURN DISTINCT p.person_id AS person_id, "
                "  p.name AS name, p.first_name AS first_name, "
                "  p.birth_year AS birth_year, "
                "  collect(DISTINCT c.name)[0..3] AS companies, "
                "  count(DISTINCT c) AS company_count "
                "LIMIT $limit",
                q=name, limit=limit,
            ).data()
        return rows
