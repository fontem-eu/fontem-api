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


def test_endpoint_pipeline_block_with_configured_source(monkeypatch):
    """The pipeline block reads etl_runs_source off app.state — and
    `configured` is a PROPERTY (calling it 500'd in prod)."""
    monkeypatch.setenv("VIRTUOSO_SPARQL_URL", "http://fake:8890/sparql")

    class _Src:
        configured = True

        def recent_runs(self, *, limit, cronjob_name):  # pylint: disable=unused-argument
            assert cronjob_name == "etl-cellar-mirror"
            return [{"status": "success", "finished_at": "2026-07-12T04:35:00Z"}]

    fake = _FakeVirtuoso()
    monkeypatch.setattr(
        "src.api.routers.legislative_dq.VirtuosoClient.from_env",
        classmethod(lambda cls: fake))
    app.state.etl_runs_source = _Src()
    try:
        r = TestClient(app).get("/data-quality/legislative")
    finally:
        app.state.etl_runs_source = None
    assert r.status_code == 200
    body = r.json()
    assert body["pipeline"]["last_run"]["status"] == "success"
