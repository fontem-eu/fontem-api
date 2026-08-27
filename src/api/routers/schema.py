"""The graph's shape, served to whoever has to write a query against it.

The assistant guessed all three of: the relationship direction, the country
code convention, and which companies exist. Two of the three guesses were
wrong or wasteful — a Company->Contract pattern that returns zero rows where
Contract-[:AWARDED_TO]->Company holds 188, and 24 name-by-name searches where
one country filter would do. It then created "Schema probe" queries in the
user's own Studio project, because introspection had no other outlet.

This endpoint is that outlet. It is served, not hardcoded, so it cannot rot
when the ontology moves; it is cached because the answer changes on ETL
timescales, not request timescales; and it is annotated as an agent tool so
the assistant can ask for it. The same payload feeds the assistant's system
prompt on models whose context can afford it.
"""
from __future__ import annotations

import time

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException

from src.api.agent_tools import agent_tool
from src.data.graph.neo4j_client import Neo4jClient

router = APIRouter(prefix="/schema", tags=["schema"])

#: The graph changes on ETL cadence (hours), so an answer this expensive to
#: assemble — a query per label and per relationship type — is computed once
#: and reused. Six hours is well inside the staleness a *schema* can afford:
#: labels and edge types change on deploy timescales, only the counts drift.
CACHE_TTL_SECONDS = 6 * 3600

#: How many relationship instances to sample when deriving endpoint labels.
#: One would miss types that connect several label pairs; a full scan of a
#: multi-million-edge type timed out a shell session while this was designed.
_ENDPOINT_SAMPLE = 25

#: How many nodes to sample per label for the property-key union.
_KEY_SAMPLE = 5

#: What cannot be derived: the conventions a query has to follow to return
#: anything. Each line here earns its place by having actually burned someone.
CONVENTIONS = (
    "Country codes are ISO-3166 alpha-3 in `country` properties: "
    "'RUS', 'FRA', 'DEU' — never 'RU' or 'Russia'.",
    "Monetary values are EUR in `value_eur` properties; dates are "
    "ISO-8601 strings.",
    "Contracts point at their winners: (Contract)-[:AWARDED_TO]->(Company). "
    "Buyers point at contracts: (Authority)-[:AWARDED]->(Contract). "
    "Company->Contract patterns return nothing.",
    "SAME_AS, LISTED_AS, REPORTED and CATEGORIZED_AS are internal "
    "bookkeeping edges, not analytical relationships.",
    "CLIENT_OF and SUPPLIER_OF are retired summary edges; any survivors are "
    "stale. Use the per-contract AWARDED / AWARDED_TO edges, which carry a "
    "time dimension.",
)

_cache: dict = {"at": 0.0, "payload": None}


def _derive(client: Neo4jClient) -> dict:
    """One pass over the graph's metadata, all queries bounded.

    Counts come from Neo4j's count store (O(1)); label pairs and property
    keys come from LIMIT-bounded samples. Nothing here scans an edge type in
    full — that is exactly the mistake this endpoint exists to prevent.
    """
    with client.session() as session:
        labels = [r["label"] for r in session.run(
            "CALL db.labels() YIELD label RETURN label ORDER BY label")]
        rel_types = [r["relationshipType"] for r in session.run(
            "CALL db.relationshipTypes() YIELD relationshipType "
            "RETURN relationshipType ORDER BY relationshipType")]

        nodes = []
        for label in labels:
            count = session.run(
                f"MATCH (n:`{label}`) RETURN count(n) AS c").single()["c"]
            keys: set[str] = set()
            for row in session.run(
                    f"MATCH (n:`{label}`) RETURN keys(n) AS k "
                    f"LIMIT {_KEY_SAMPLE}"):
                keys.update(row["k"])
            nodes.append({"label": label, "count": count,
                          "keys": sorted(keys)})

        relationships = []
        for rel in rel_types:
            count = session.run(
                f"MATCH ()-[r:`{rel}`]->() RETURN count(r) AS c"
            ).single()["c"]
            pairs = set()
            for row in session.run(
                    f"MATCH (a)-[:`{rel}`]->(b) "
                    f"RETURN labels(a)[0] AS f, labels(b)[0] AS t "
                    f"LIMIT {_ENDPOINT_SAMPLE}"):
                pairs.add((row["f"], row["t"]))
            for f, t in sorted(pairs):
                relationships.append(
                    {"type": rel, "from": f, "to": t, "count": count})

    return {
        "node_labels": nodes,
        "relationships": relationships,
        "conventions": list(CONVENTIONS),
    }


@router.get(
    "/graph",
    responses={503: {"description": "The graph store did not answer; "
                                    "retry after a moment."}},
    openapi_extra=agent_tool(
        name="get_schema",
        when=(
            "Returns the graph's schema: every node label with its "
            "property keys and count, every relationship type WITH its "
            "direction (from-label to to-label), and the query conventions "
            "(country code format, value units, which edges are "
            "bookkeeping). Call this BEFORE writing any graph query."
        ),
        group="docs",
        core=True,
    ),
)
@inject
async def graph_schema(client: FromDishka[Neo4jClient]) -> dict:
    """The live graph schema, cached server-side."""
    now = time.time()
    if _cache["payload"] is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
        return _cache["payload"]
    try:
        payload = _derive(client)
    except Exception as exc:  # pylint: disable=broad-except
        # A stale schema beats no schema: keep serving the previous payload
        # past its TTL rather than failing the caller on a graph hiccup.
        if _cache["payload"] is not None:
            return _cache["payload"]
        raise HTTPException(
            status_code=503,
            detail=f"graph store unavailable: {exc}") from exc
    payload["cached_at"] = now
    _cache["payload"] = payload
    _cache["at"] = now
    return payload
