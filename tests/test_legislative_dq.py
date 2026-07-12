"""Legislative-mirror DQ endpoint: graph-scoped stats, 503 without
Virtuoso configured (gitops#290)."""
from fastapi.testclient import TestClient

from src.api.app import app
from src.api.routers.legislative_dq import GRAPH, get_legislative_stats


class _FakeVirtuoso:
    """Answers each stats query by keyword."""

    def __init__(self):
        self.queries = []

    _ANSWERS = (
        ("?year", [{"year": "2023", "works": "9"},
                   {"year": "2024", "works": "11"}]),
        ("?decade", [{"decade": "1950s", "works": "12"},
                     {"decade": "1960s", "works": "28"}]),
        ("?triples", [{"triples": "1000"}]),
        ("?expressions", [{"expressions": "300"}]),
        ("?manifestations", [{"manifestations": "600"}]),
        ("?earliest", [{"earliest": "1952-07-23", "latest": "1983-12-19"}]),
        ("?with_eli", [{"with_eli": "30"}]),
        ("?works", [{"works": "40"}]),
    )

    def query(self, q):
        self.queries.append(q)
        for needle, rows in self._ANSWERS:
            if needle in q:
                return rows
        raise AssertionError(f"unexpected query: {q}")


def test_stats_shape_and_graph_scoping():
    stats = get_legislative_stats(_FakeVirtuoso())
    assert stats["graph"] == GRAPH
    assert stats["triples"] == 1000
    assert stats["works"] == 40
    assert stats["expressions"] == 300
    assert stats["manifestations"] == 600
    assert stats["earliest_work_date"] == "1952-07-23"
    assert stats["latest_work_date"] == "1983-12-19"
    assert stats["eli_coverage"] == 0.75
    assert stats["works_by_decade"][0] == {"decade": "1950s", "works": 12}
    assert stats["works_by_year"][-1] == {"year": "2024", "works": 11}


def test_every_query_is_graph_scoped():
    """No full-store scans — every SPARQL carries FROM <mirror graph>
    (the Virtuoso OOM discipline applies to reads)."""
    fake = _FakeVirtuoso()
    get_legislative_stats(fake)
    assert fake.queries
    for q in fake.queries:
        assert f"FROM <{GRAPH}>" in q


def test_endpoint_503_without_virtuoso(monkeypatch):
    monkeypatch.delenv("VIRTUOSO_SPARQL_URL", raising=False)
    r = TestClient(app).get("/data-quality/legislative")
    assert r.status_code == 503
