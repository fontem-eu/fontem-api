"""Unit tests for the dirty-set consumer's per-entity decision logic.

End-to-end the consumer touches Postgres, HTTP, and Virtuoso — none
of which belong in a unit test. We test ``process_one`` with fakes
for each dependency to pin the branching: tombstone → DELETE only,
OK → fetch+write+success, REDIRECT → write+success without
preflagging the survivor, NOT_FOUND → outcome says leave-pending.

Postgres clear_dirty is now done in a single batched call after the
ThreadPool joins, so per-entity tests don't assert anything about
pg connections — the run_batch-level test covers that.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from rdflib import Graph, URIRef

from src.relay.wikidata_consumer import process_one
from src.relay.wikidata_fetcher import FetchOutcome, FetchResult


@dataclass
class _HttpFake:
    pass


@dataclass
class _WrittenAtom:
    entity_id: str
    triples: int


class _Recorder:
    """Captures fetcher / writer / tombstone calls so each test can
    assert what happened."""

    def __init__(self):
        self.fetched: list[str] = []
        self.written: list[_WrittenAtom] = []
        self.tombstoned: list[str] = []
        self.fetch_result: FetchResult | None = None

    def fetch(self, entity_id, _client):
        self.fetched.append(entity_id)
        assert self.fetch_result is not None
        return self.fetch_result

    def write(self, entity_id, filtered, _client, _url, _auth):
        self.written.append(_WrittenAtom(entity_id, len(filtered)))

    def tombstone(self, entity_id, _client, _url, _auth):
        self.tombstoned.append(entity_id)


@pytest.fixture(name="rec")
def fixture_rec(monkeypatch) -> _Recorder:
    r = _Recorder()
    monkeypatch.setattr("src.relay.wikidata_consumer.fetch_truthy", r.fetch)
    monkeypatch.setattr("src.relay.wikidata_consumer.write_entity", r.write)
    monkeypatch.setattr("src.relay.wikidata_consumer.tombstone_entity",
                        r.tombstone)
    return r


def test_tombstone_path_skips_fetch_and_returns_tombstoned(rec) -> None:
    outcome, eid, lc = process_one(
        "Q42", "ts", is_deleted=True,
        http_client=_HttpFake(),
        sparql_url="http://v/sparql-auth", auth=("dba", "x"),
    )
    assert outcome == "tombstoned"
    assert (eid, lc) == ("Q42", "ts")
    assert rec.tombstoned == ["Q42"]
    assert rec.fetched == []
    assert rec.written == []


def test_ok_path_writes_and_returns_written(rec) -> None:
    g = Graph()
    g.add((URIRef("http://www.wikidata.org/entity/Q42"),
           URIRef("http://www.wikidata.org/prop/direct/P31"),
           URIRef("http://www.wikidata.org/entity/Q5")))
    rec.fetch_result = FetchResult(FetchOutcome.OK, "Q42", g, None)
    outcome, eid, lc = process_one(
        "Q42", "ts", is_deleted=False,
        http_client=_HttpFake(),
        sparql_url="http://v/sparql-auth", auth=("dba", "x"),
    )
    assert outcome == "written"
    assert (eid, lc) == ("Q42", "ts")
    assert rec.fetched == ["Q42"]
    assert [w.entity_id for w in rec.written] == ["Q42"]


def test_redirect_path_writes_survivor_graph(rec) -> None:
    g = Graph()
    g.add((URIRef("http://www.wikidata.org/entity/Q1234"),
           URIRef("http://www.w3.org/2002/07/owl#sameAs"),
           URIRef("http://www.wikidata.org/entity/Q5678")))
    rec.fetch_result = FetchResult(FetchOutcome.REDIRECT, "Q1234", g, "Q5678")
    outcome, eid, _ = process_one(
        "Q1234", "ts", is_deleted=False,
        http_client=_HttpFake(),
        sparql_url="http://v/sparql-auth", auth=("dba", "x"),
    )
    assert outcome == "redirected"
    assert eid == "Q1234"
    # The graph for the redirected id is written — Virtuoso gets the
    # owl:sameAs triple so SPARQL clients can follow the link.
    assert [w.entity_id for w in rec.written] == ["Q1234"]


def test_not_found_returns_left_pending_and_does_not_write(rec) -> None:
    rec.fetch_result = FetchResult(FetchOutcome.NOT_FOUND, "Q99", None, None)
    outcome, eid, _ = process_one(
        "Q99", "ts", is_deleted=False,
        http_client=_HttpFake(),
        sparql_url="http://v/sparql-auth", auth=("dba", "x"),
    )
    assert outcome == "not_found_left_pending"
    assert eid == "Q99"
    assert rec.written == []
    assert rec.tombstoned == []
