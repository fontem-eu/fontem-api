"""Tests for the CELEX spine materializer + petition linker (P1/P2)."""
from __future__ import annotations

from src.etl.legislative.materialize_legal_acts import (
    answer_ref_to_celex,
    doc_type,
    link_petitions,
    materialize_spine,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows

    def single(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, store):
        self.store = store

    def run(self, query, **params):
        self.store.append((query, params))
        if "MATCH (p:Petition)" in query:
            return _FakeResult(self.store_petitions)
        return _FakeResult([])

    store_petitions: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeDriver:
    def __init__(self, petitions=None):
        self.calls: list = []
        _FakeSession.store_petitions = petitions or []

    def session(self):
        return _FakeSession(self.calls)


def test_doc_type():
    assert doc_type("32024L1385") == "Directive"
    assert doc_type("32024D1824") == "Decision"
    assert doc_type("52026DC4110") == "Preparatory act"


def test_answer_ref_to_celex():
    assert answer_ref_to_celex("C(2026)4110") == "52026DC4110"
    assert answer_ref_to_celex("C(2026)411") == "52026DC0411"
    assert answer_ref_to_celex("COM(2026)1") is None


def test_spine_pages_until_short_page(monkeypatch):
    pages = [
        [{"celex": "32024D0001", "date": "2024-01-01"},
         {"celex": "32024D0002"}],
        [],
    ]
    calls = []

    def fake_sparql(_endpoint, query):
        calls.append(query)
        return pages.pop(0)

    monkeypatch.setattr(
        "src.etl.legislative.materialize_legal_acts.sparql", fake_sparql)
    monkeypatch.setattr(
        "src.etl.legislative.materialize_legal_acts.PAGE", 2)
    driver = _FakeDriver()
    n = materialize_spine("http://x", driver)
    assert n == 2
    # keyset: second query filters past the last celex of page one
    assert '> "32024D0002"' in calls[1]
    # a MERGE batch was written
    assert any("MERGE (a:LegalAct" in q for q, _ in driver.calls)


def test_link_petitions_only_links_resolved(monkeypatch):
    petitions = [{
        "system": "eu-eci", "pid": "ECI(2024)000007",
        "reg": "32024D1824", "refs": ["C(2026)4110", "C(1900)1"],
    }]

    def fake_sparql(_endpoint, _query):
        # only the registration decision exists in the mirror
        return [{"celex": "32024D1824", "date": "2024-06-17",
                 "title": "Commission Decision ..."}]

    monkeypatch.setattr(
        "src.etl.legislative.materialize_legal_acts.sparql", fake_sparql)
    driver = _FakeDriver(petitions)
    stats = link_petitions("http://x", driver)
    assert stats["resolved"] == 1
    assert stats["edges"] == 1
    assert stats["unresolved"] == 2
    joined = " ".join(q for q, _ in driver.calls)
    assert "REGISTERED_BY" in joined
    assert "ANSWERED_BY" not in joined
