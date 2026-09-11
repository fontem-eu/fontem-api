"""Rebuilding the company subjects that lost their name.

2,579,228 prod company subjects have no rdfs:label, 2,359,235 of them
nothing but a type. Neo4j holds every sampled one intact. What follows
pins the selection (a lookup, both subject kinds), the aiming (which
subject each rebuild lands on, and what it must not destroy on the way)
and the pace.
"""
# pylint: disable=protected-access
from unittest.mock import MagicMock

from fontem_event_schemas import builders, validate

from src.etl import rebuild_stripped_companies as rsc

_C = rsc._COMPANY_PREFIX
_F = f"{rsc._ID_PREFIX}InvestmentFund/"


class _Virtuoso:
    """Keyset pages over label-less subjects, and per-subject predicate
    lookups answered from ``held`` ({subject IRI: [predicate, ...]})."""

    def __init__(self, subjects, held=None):
        self._subjects = sorted(subjects)
        self._held = held or {}
        self.queries = []

    def query(self, q):
        self.queries.append(q)
        if "VALUES ?s" in q:
            iris = [i.strip("<>") for i in
                    q.split("VALUES ?s {")[1].split("}")[0].split()]
            return [{"s": i, "p": p} for i in iris for p in self._held.get(i, ())]
        limit = int(q.split("LIMIT")[1].split()[0])
        after = q.split('STR(?s) > "')[1].split('"')[0]
        return [{"s": s} for s in self._subjects if s > after][:limit]


def _neo4j(records):
    client = MagicMock()
    session = client.session.return_value
    session.__enter__ = lambda s: s
    session.__exit__ = lambda s, *a: None

    def _run(query, **_kw):
        label = query.split("MATCH (c:")[1].split(")")[0]
        res = MagicMock()
        res.data.return_value = [
            {"gmr_id": r["gmr_id"], "labels": r["labels"],
             "props": {k: v for k, v in r.items() if k != "labels"}}
            for r in records if label in r["labels"]
        ]
        return res

    session.run.side_effect = _run
    return client


def _node(gid, labels, kind, **extra):
    return {"gmr_id": gid, "labels": labels, "name": f"Entity {gid}",
            "country": "LUX", "active": True, "entity_kind": kind, **extra}


def _refs(virtuoso, page=5_000):
    return [ref for _, refs in rsc.iter_labelless(virtuoso, page) for ref in refs]


def test_selection_is_a_label_lookup():
    """The literal test -- "no predicate but rdf:type" -- returned nothing
    in 500s on prod. A single-predicate NOT EXISTS answers 5,000 rows in
    12.5s."""
    v = _Virtuoso([f"{_C}a"])
    _refs(v)
    q = v.queries[0]
    assert f"FILTER NOT EXISTS {{ ?s <{rsc._RDFS_LABEL}> ?l }}" in q
    assert "!=" not in q
    assert "COUNT(" not in q and "GROUP BY" not in q


def test_both_subject_kinds_are_selected_and_nothing_else():
    v = _Virtuoso([f"{_C}a", f"{_C}b", f"{_F}c",
                   "http://data.fontem.eu/id/Listing/X", f"{_C}d/extra"])
    assert _refs(v, page=2) == [
        ("Company", "a"), ("Company", "b"), ("InvestmentFund", "c"),
    ]
    assert len(v.queries) >= 3


def test_a_restart_can_resume_past_a_logged_key():
    v = _Virtuoso([f"{_C}a", f"{_C}b", f"{_C}c"])
    refs = [r for _, rs in rsc.iter_labelless(v, 10, start_after=f"{_C}a") for r in rs]
    assert refs == [("Company", "b"), ("Company", "c")]


def test_a_page_at_the_cap_is_refused():
    class _Truncating:
        def query(self, _q):
            return [{"s": f"{_C}{i}"} for i in range(5)]

    try:
        list(rsc.iter_labelless(_Truncating(), page=4, cap=5))
    except RuntimeError as exc:
        assert "cap" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError at the result-set cap")


def test_a_company_is_rebuilt_without_entity_kind():
    payloads, skipped = rsc.load_for_labelless(
        _neo4j([_node("a", ["Company"], "GENERAL")]), [("Company", "a")])
    assert [p["gmr_id"] for p in payloads] == ["a"]
    assert "entity_kind" not in payloads[0]["identity"]
    assert not any(skipped.values())


def test_a_fund_is_rebuilt_the_way_load_gleif_writes_it():
    """entity_kind FUND at the Company IRI: the sink refreshes the
    InvestmentFund subject and drops any Company twin. Skipping funds, as
    the stripped repair does, would leave 154,648 of them nameless."""
    payloads, _ = rsc.load_for_labelless(
        _neo4j([_node("f", ["InvestmentFund"], "FUND")]),
        [("InvestmentFund", "f")])
    event_payload = builders.upsert_company(**payloads[0])
    validate("UpsertCompany", 1, event_payload)
    assert event_payload["entity_kind"] == "FUND"

    log = MagicMock()
    emit = log.batch.return_value.__enter__.return_value
    rsc.emit_rebuilds(log, payloads)
    assert emit.upsert.call_args.kwargs["iri"] == f"{_C}f"


def test_a_fund_is_recognised_by_its_label_even_without_a_kind():
    payloads, _ = rsc.load_for_labelless(
        _neo4j([_node("f", ["InvestmentFund"], None)]), [("Company", "f")])
    assert payloads[0]["identity"]["entity_kind"] == "FUND"


def test_a_stale_fund_twin_is_dropped_with_the_nodes_own_kind():
    payloads, _ = rsc.load_for_labelless(
        _neo4j([_node("t", ["Company"], "GENERAL")]), [("InvestmentFund", "t")])
    assert payloads[0]["identity"]["entity_kind"] == "GENERAL"


def test_a_stale_twin_with_no_kind_is_reported_not_guessed():
    payloads, skipped = rsc.load_for_labelless(
        _neo4j([_node("t", ["Company"], None)]), [("InvestmentFund", "t")])
    assert not payloads
    assert skipped[rsc.UNKINDED_TWIN] == ["t"]


def test_missing_and_nameless_nodes_are_reported():
    payloads, skipped = rsc.load_for_labelless(
        _neo4j([_node("n", ["Company"], None, name=None)]),
        [("Company", "n"), ("Company", "gone")])
    assert not payloads
    assert skipped[rsc.NAMELESS] == ["n"]
    assert skipped[rsc.MISSING] == ["gone"]


_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"
_SUBSIDIARY_OF = "http://data.fontem.eu/ontology#subsidiaryOf"


def _run(held, nodes, subjects):
    log = MagicMock()
    emit = log.batch.return_value.__enter__.return_value
    totals = rsc.run_labelless(_Virtuoso(subjects, held), _neo4j(nodes), log)
    return [c.kwargs["payload"]["gmr_id"] for c in emit.upsert.call_args_list], totals


def test_a_fund_rebuild_that_would_orphan_an_equivalence_is_held_back():
    emitted, totals = _run({f"{_C}f": [rsc._RDF_TYPE, _SAME_AS]},
                           [_node("f", ["InvestmentFund"], "FUND")], [f"{_C}f"])
    assert not emitted
    assert totals[rsc.HELD_WOULD_LOSE] == 1


def test_a_fund_rebuild_that_would_delete_relationship_edges_is_held_back():
    """fontem:subsidiaryOf lives on the subject: 29 of 40 label-less
    subjects sampled on shared carry it. The relabel deletes would take
    it with them and the rebuild writes only company fields back."""
    emitted, totals = _run(
        {f"{_F}f": [rsc._RDF_TYPE, f"{rsc._FONTEM}lei", _SUBSIDIARY_OF]},
        [_node("f", ["InvestmentFund"], "FUND")], [f"{_F}f"])
    assert not emitted
    assert totals[rsc.HELD_WOULD_LOSE] == 1


def test_a_fund_carrying_only_rewritten_fields_is_rebuilt():
    emitted, totals = _run(
        {f"{_F}f": [rsc._RDF_TYPE, f"{rsc._FONTEM}lei"],
         f"{_C}f": [rsc._RDF_TYPE, rsc._RDFS_LABEL, rsc._WDT_P17]},
        [_node("f", ["InvestmentFund"], "FUND")], [f"{_F}f"])
    assert emitted == ["f"]
    assert totals["funds"] == 1


def test_a_company_rebuild_is_never_held_back():
    """No entity_kind: the sink replaces only the fields the event states,
    so the subject's edges and equivalences survive the rebuild."""
    emitted, totals = _run({f"{_C}a": [rsc._RDF_TYPE, _SAME_AS, _SUBSIDIARY_OF]},
                           [_node("a", ["Company"], "GENERAL")], [f"{_C}a"])
    assert emitted == ["a"]
    assert totals[rsc.HELD_WOULD_LOSE] == 0


def test_what_a_rebuild_writes_back_excludes_edges_and_equivalences():
    assert rsc._RDFS_LABEL in rsc._REWRITTEN and rsc._RDF_TYPE in rsc._REWRITTEN
    assert _SAME_AS not in rsc._REWRITTEN
    assert _SUBSIDIARY_OF not in rsc._REWRITTEN


def test_a_dry_run_emits_nothing(monkeypatch):
    virtuoso_cls = MagicMock()
    virtuoso_cls.from_env.return_value = _Virtuoso([f"{_C}a", f"{_F}f"])
    monkeypatch.setattr(rsc, "VirtuosoClient", virtuoso_cls)
    monkeypatch.setattr(rsc, "Neo4jClient", lambda *a, **k: _neo4j([
        _node("a", ["Company"], "GENERAL"), _node("f", ["InvestmentFund"], "FUND"),
    ]))
    event_log_cls = MagicMock()
    monkeypatch.setattr(rsc, "EventLog", event_log_cls)
    rsc.main(["--select", "labelless"])
    event_log_cls.from_env.assert_not_called()


def test_apply_emits_through_the_rate_limit(monkeypatch):
    virtuoso_cls = MagicMock()
    virtuoso_cls.from_env.return_value = _Virtuoso([f"{_C}a"])
    monkeypatch.setattr(rsc, "VirtuosoClient", virtuoso_cls)
    monkeypatch.setattr(rsc, "Neo4jClient",
                        lambda *a, **k: _neo4j([_node("a", ["Company"], None)]))
    event_log_cls = MagicMock()
    monkeypatch.setattr(rsc, "EventLog", event_log_cls)
    consumed = []
    monkeypatch.setattr(rsc.RateLimit, "consumed", lambda self, n: consumed.append(n))
    rsc.main(["--select", "labelless", "--apply"])
    emit = event_log_cls.from_env.return_value.batch.return_value.__enter__.return_value
    assert emit.upsert.call_count == 1
    assert consumed == [1]


def test_limit_stops_the_scan():
    v = _Virtuoso([f"{_C}{i}" for i in "abcdef"])
    neo = _neo4j([_node(i, ["Company"], None) for i in "abcdef"])
    totals = rsc.run_labelless(v, neo, None, page=2, limit=3)
    assert totals["selected"] == 3
    assert totals["rebuilt"] == 3


def test_the_rate_is_held_across_the_whole_run():
    now = [0.0]
    slept = []
    limit = rsc.RateLimit(30.0, clock=lambda: now[0], sleep=slept.append)
    limit.consumed(30)
    limit.consumed(30)
    assert slept == [1.0, 2.0]
    now[0] = 10.0
    limit.consumed(30)
    assert len(slept) == 2, "already behind the rate: no sleep"
