"""Resolving an entity's identity class inside a Cypher query.

The consolidator decides that two records are the same real-world
entity. That decision reaches Virtuoso as ``owl:sameAs`` and Neo4j as a
``:SAME_AS`` edge, from the same ``AssertSameAs`` event. A read that
does not follow it shows one record's contracts and silently omits its
duplicates' -- which is what a company or authority page did before
this module existed. eu-LISA is the small, legible case: two Authority
nodes, three contracts on one and one on the other, so whichever record
you landed on decided what you saw.

Why a graph store can do this without a reasoner
------------------------------------------------
Virtuoso resolves the class with the property path
``(owl:sameAs|^owl:sameAs)*``. That looks like inference and is not:
nothing propagates properties across the edge. It is transitive
reachability over one predicate, in both directions, reflexive --
ordinary graph traversal, which is exactly what Neo4j is for.

Why apoc.path.subgraphNodes and not ``-[:SAME_AS*0..]-``
--------------------------------------------------------
The obvious rendering is wrong at scale. SPARQL's ``*`` is defined as
reachability and yields distinct nodes; Cypher's ``*`` enumerates
PATHS. On shared's densest identity class (max degree 40, and cyclic)
the variable-length form had to be killed after minutes, while the BFS
below returned the same class -- 47 nodes -- immediately. BFS visits
each node once, so the cost is the size of the class, not the number of
walks through it.

Why the relationship TYPE and not a status property
---------------------------------------------------
``:SAME_AS_CANDIDATE`` carries 39,193 approved alongside 304,702
pending proposals. Traversing that type and filtering on the property
would merge roughly eight times too much the first time a query forgets
the predicate -- and APOC's relationshipFilter cannot express a property
filter at all. Only asserted equivalences ever get ``:SAME_AS``, so the
type IS the filter.
"""
from __future__ import annotations

#: The identity relationship. Only ever written for an asserted
#: equivalence (fontem-neo4j-sink#148); proposals stay
#: :SAME_AS_CANDIDATE and never reach this traversal.
SAME_AS = "SAME_AS"


def identity_class(
    label: str, id_prop: str, *, param: str = "gid", out: str = "me",
) -> str:
    """Cypher that binds ``out`` to each member of the entity's identity
    class, the seed included.

    Emits a MATCH plus an APOC BFS, so the caller continues with ``out``
    bound to one member per row -- drop it straight into the position
    the single-node MATCH used to hold::

        identity_class("Company", "gmr_id") +
        "MATCH (ct:Contract)-[:AWARDED_TO]->(me) "
        "WITH DISTINCT ct "
        "RETURN count(ct) AS n"

    The ``WITH DISTINCT`` is not optional. A contract awarded to two
    members of one class is reachable twice, and every aggregate
    downstream -- counts, value sums, row lists -- would double it.

    An entity with no ``:SAME_AS`` edges yields exactly itself, so a
    caller needs no special case for the overwhelmingly common
    unmerged entity.
    """
    return (
        f"MATCH (_seed:{label} {{{id_prop}: ${param}}}) "
        f"CALL apoc.path.subgraphNodes(_seed, {{"
        f"relationshipFilter: '{SAME_AS}', maxLevel: -1"
        f"}}) YIELD node AS {out} "
    )
