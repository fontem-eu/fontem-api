"""Read-only Data Studio query proxies: /query/cypher + /query/sql."""
# pylint: disable=missing-class-docstring,missing-function-docstring,unused-argument,too-few-public-methods
from __future__ import annotations

from tests.dishka_fixtures import make_test_client, cleanup_dishka


class _Result:
    def __init__(self, cols, records):
        self._cols = cols
        self._records = records

    def keys(self):
        return self._cols

    def __iter__(self):
        return iter(self._records)


class _Tx:
    def __init__(self, cols, records):
        self._c, self._r = cols, records

    def run(self, query, **kwargs):
        return _Result(self._c, self._r)


class _Session:
    def __init__(self, cols, records):
        self._c, self._r = cols, records

    def execute_read(self, fn):
        return fn(_Tx(self._c, self._r))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Neo4j:
    def __init__(self, cols, records):
        self._c, self._r = cols, records

    def session(self):
        return _Session(self._c, self._r)

    def close(self):
        pass


def _client(cols=("n",), records=None):
    return make_test_client(neo4j_client=_Neo4j(list(cols), records or []))


def test_cypher_read_returns_columns_and_rows():
    c = _client(cols=("name", "n"), records=[{"name": "A", "n": 3}, {"name": "B", "n": 1}])
    try:
        r = c.post("/query/cypher",
                   json={"query": "MATCH (co) RETURN co.name AS name, 1 AS n"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["columns"] == ["name", "n"]
        assert body["rows"] == [["A", 3], ["B", 1]]
        assert body["truncated"] is False
    finally:
        cleanup_dishka()


def test_cypher_rejects_writes():
    c = _client()
    try:
        for q in ("CREATE (n:X)", "MATCH (n) DELETE n", "MATCH (n) SET n.x = 1", "MERGE (n:Y)"):
            assert c.post("/query/cypher", json={"query": q}).status_code == 400
    finally:
        cleanup_dishka()


def test_query_rejects_empty_and_oversized():
    c = _client()
    try:
        assert c.post("/query/cypher", json={"query": ""}).status_code == 400
        assert c.post("/query/cypher", json={"query": "RETURN " + "x" * 9000}).status_code == 400
    finally:
        cleanup_dishka()


def test_sql_rejects_writes_before_touching_db():
    c = _client()
    try:
        for q in ("DROP TABLE t", "INSERT INTO t VALUES (1)",
                  "UPDATE t SET x=1", "TRUNCATE t"):
            assert c.post("/query/sql", json={"query": q}).status_code == 400
    finally:
        cleanup_dishka()


def test_sql_unconfigured_reports_503(monkeypatch):
    monkeypatch.delenv("STATS_DATABASE_URL", raising=False)
    c = _client()
    try:
        r = c.post("/query/sql", json={"query": "SELECT 1"})
        assert r.status_code == 503
    finally:
        cleanup_dishka()


# ── Schema introspection ────────────────────────────────────────────
class _SchemaResult:
    def __init__(self, cols, records):
        self._cols, self._records = cols, records

    def keys(self):
        return self._cols

    def __iter__(self):
        return iter(self._records)


class _SchemaTx:
    def run(self, query, **kwargs):
        q = query.lower()
        if "db.labels" in q:
            return _SchemaResult(["label"], [{"label": "Company"}, {"label": "Contract"}])
        if "relationshiptypes" in q:
            return _SchemaResult(["relationshipType"], [{"relationshipType": "AWARDED_TO"}])
        if "nodetypeproperties" in q:
            return _SchemaResult(
                ["nodeLabels", "propertyName"],
                [{"nodeLabels": ["Company"], "propertyName": "name"},
                 {"nodeLabels": ["Company"], "propertyName": "lei"},
                 {"nodeLabels": ["Contract"], "propertyName": "value"}],
            )
        if "propertykeys" in q:
            return _SchemaResult(["propertyKey"], [{"propertyKey": "name"}])
        return _SchemaResult([], [])


class _SchemaSession:
    def execute_read(self, fn):
        return fn(_SchemaTx())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _SchemaNeo4j:
    def session(self):
        return _SchemaSession()

    def close(self):
        pass


class _FakeVirtuoso:
    def query(self, q):
        if "?c" in q:
            return [{"c": "https://schema.org/Organization"},
                    {"c": "http://data.fontem.eu/Company"}]
        return [{"p": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"}]


def _clear_schema_cache():
    from src.api.routers.query import _SCHEMA_CACHE  # pylint: disable=import-outside-toplevel
    _SCHEMA_CACHE.clear()


def test_cypher_schema_introspection():
    _clear_schema_cache()
    c = make_test_client(neo4j_client=_SchemaNeo4j())
    try:
        r = c.get("/query/schema/cypher")
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["labels"] == ["Company", "Contract"]
        assert b["relationshipTypes"] == ["AWARDED_TO"]
        assert b["labelProperties"]["Company"] == ["lei", "name"]
        assert "value" in b["properties"] and "name" in b["properties"]
    finally:
        cleanup_dishka()


def test_sparql_schema_with_client_and_without():
    _clear_schema_cache()
    c = make_test_client(virtuoso=_FakeVirtuoso())
    try:
        b = c.get("/query/schema/sparql").json()
        assert "https://schema.org/Organization" in b["classes"]
        assert any("rdf-syntax" in p for p in b["predicates"])
    finally:
        cleanup_dishka()
    _clear_schema_cache()
    c = make_test_client()  # virtuoso defaults to None → empty, still 200
    try:
        b = c.get("/query/schema/sparql").json()
        assert b == {"lang": "sparql", "classes": [], "predicates": []}
    finally:
        cleanup_dishka()


def test_sql_schema_unconfigured_503(monkeypatch):
    _clear_schema_cache()
    monkeypatch.delenv("STATS_DATABASE_URL", raising=False)
    c = make_test_client()
    try:
        assert c.get("/query/schema/sql").status_code == 503
    finally:
        cleanup_dishka()


def test_schema_unknown_lang_400():
    c = make_test_client()
    try:
        assert c.get("/query/schema/klingon").status_code == 400
    finally:
        cleanup_dishka()
