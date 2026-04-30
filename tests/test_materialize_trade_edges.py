"""Regression tests for materialize_trade_edges.

The materialise job builds CLIENT_OF / SUPPLIER_OF summary edges
between Authority and Company nodes by aggregating the
``Authority -[:AWARDED]-> Contract -[:AWARDED_TO]-> Company`` chain
into a single weighted edge per trade pair.

The graph view defaults to rendering these summary edges, while the
contracts list runs straight off AWARDED. The two views diverge if
the summary stops tracking the underlying graph — the user-visible
incident: eu-LISA showed 4 contracts in the graph view (via stale
CLIENT_OF edges) and 0 in the contracts list (live AWARDED query).

These tests pin the invariants that keep the two views consistent
once the job runs:

  1. Every full run DROPS existing CLIENT_OF / SUPPLIER_OF before
     rebuilding from AWARDED, so orphan summary edges from prior
     state get pruned (defence-in-depth against consolidation
     paths that don't update them inline).
  2. Both edge directions are written for every (Authority, Company)
     pair — the graph view depends on having both for symmetric
     navigation.
  3. The aggregate is non-trivial: contract count and total_eur
     must aggregate against the input rows, not pass through.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import src.etl.materialize_trade_edges as materialize


def _capturing_session():
    """Stub Neo4j session that records every Cypher query + params."""
    captured: list[dict] = []

    class _Result:
        def __init__(self, rows=None):
            self._rows = rows or []
        def data(self):
            return self._rows

    def _run(cypher, **kwargs):
        captured.append({"cypher": cypher, "params": kwargs})
        # The aggregate query returns rows; the DELETEs and CREATEs
        # don't. Match by signature.
        if "RETURN a.authority_id" in cypher:
            return _Result([
                {"auth_id": "auth-1", "company_id": "co-1",
                 "contracts": 3, "total_eur": 1500.0,
                 "earliest": "2024-01-01", "latest": "2024-12-01"},
                {"auth_id": "auth-1", "company_id": "co-2",
                 "contracts": 1, "total_eur": 500.0,
                 "earliest": "2024-06-01", "latest": "2024-06-01"},
            ])
        return _Result()

    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=None)
    session.run = _run
    return session, captured


def _capturing_driver(session):
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    return driver


def test_materialise_drops_before_rebuilding():
    """The first two writes must be the DROP statements — without
    them, a re-run accumulates duplicate summary edges and the
    `contracts` field gets nondeterministic depending on which row
    the engine resolves first."""
    session, captured = _capturing_session()
    materialize.materialize(_capturing_driver(session))

    deletes = [c for c in captured if "DELETE r" in c["cypher"]]
    aggregates = [c for c in captured if "RETURN a.authority_id" in c["cypher"]]

    assert len(deletes) == 2, "must drop both CLIENT_OF and SUPPLIER_OF before rebuilding"
    assert "[r:CLIENT_OF]" in deletes[0]["cypher"]
    assert "[r:SUPPLIER_OF]" in deletes[1]["cypher"]
    # Aggregate must come after the deletes — pin order so a future
    # refactor doesn't rebuild-then-delete and leave the graph empty.
    delete_idx = max(captured.index(d) for d in deletes)
    aggregate_idx = captured.index(aggregates[0])
    assert delete_idx < aggregate_idx


def test_materialise_uses_batched_delete_subqueries():
    """Regression for the OOM that killed the bootstrap Job in prod:
    a single-transaction DELETE of millions of summary edges blew
    past Neo4j's `db.memory.transaction.max` (256 MiB default) and
    failed with a TransientError. Both DROPs must use
    `CALL { ... } IN TRANSACTIONS` so the deletes commit in chunks."""
    session, captured = _capturing_session()
    materialize.materialize(_capturing_driver(session))

    deletes = [c for c in captured if "DELETE r" in c["cypher"]]
    for d in deletes:
        cypher = d["cypher"]
        assert "CALL { WITH r DELETE r }" in cypher, (
            f"DELETE not batched in transactions: {cypher!r} — this "
            "blew the prod transaction memory limit on the trade-edges "
            "bootstrap. Use CALL { ... } IN TRANSACTIONS OF N ROWS."
        )
        assert "IN TRANSACTIONS OF" in cypher


def test_materialise_writes_both_edge_directions():
    """Each pair gets a CLIENT_OF (Authority->Company) AND a SUPPLIER_OF
    (Company->Authority). Without both, the graph view's bi-directional
    expand/collapse from either side stops finding the trade pair."""
    session, captured = _capturing_session()
    materialize.materialize(_capturing_driver(session))

    creates = [c for c in captured if "CREATE (a)-[:CLIENT_OF" in c["cypher"]]
    assert creates, "no CREATE batch ran"
    cypher = creates[0]["cypher"]
    assert "CREATE (a)-[:CLIENT_OF" in cypher
    assert "CREATE (c)-[:SUPPLIER_OF" in cypher
    # Same property bag — the graph view reads `contracts` and `total_eur`
    # off either edge, so they must be symmetric.
    assert cypher.count("contracts: row.contracts") == 2
    assert cypher.count("total_eur: row.total_eur") == 2


def test_materialise_passes_aggregated_counts_through_to_create():
    """The aggregate query produces (contracts, total_eur) per pair.
    The CREATE batch must write that aggregate as the edge weight,
    not pass through a single contract's value (regression: a careful
    refactor once read from the wrong column)."""
    session, captured = _capturing_session()
    materialize.materialize(_capturing_driver(session))

    create = next(c for c in captured if "CREATE (a)-[:CLIENT_OF" in c["cypher"])
    # The batch param is the aggregate result list — driver receives
    # a row carrying contracts=3, total_eur=1500 for the first pair.
    # Just confirm we're sending what the aggregate produced, not
    # something we cooked up.
    assert create["params"]["batch"] == [
        {"auth_id": "auth-1", "company_id": "co-1",
         "contracts": 3, "total_eur": 1500.0,
         "earliest": "2024-01-01", "latest": "2024-12-01"},
        {"auth_id": "auth-1", "company_id": "co-2",
         "contracts": 1, "total_eur": 500.0,
         "earliest": "2024-06-01", "latest": "2024-06-01"},
    ]


def test_aggregate_query_traverses_full_chain():
    """The Cypher must traverse Authority->Contract->Company. Skipping
    the Contract step (e.g. `(a)-[]->(c)`) makes the aggregate match
    any relationship type and is what got us a stale graph in the
    eu-LISA incident."""
    session, captured = _capturing_session()
    materialize.materialize(_capturing_driver(session))

    aggregate = next(c for c in captured if "RETURN a.authority_id" in c["cypher"])
    assert "(a:Authority)-[:AWARDED]->(ct:Contract)-[:AWARDED_TO]->(c:Company)" in aggregate["cypher"]
    assert "count(ct) AS contracts" in aggregate["cypher"]
    assert "sum(COALESCE(ct.value_eur, 0)) AS total_eur" in aggregate["cypher"]
