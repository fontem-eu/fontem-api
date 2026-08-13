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
    def __init__(self, cols, records, spy=None):
        self._c, self._r = cols, records
        self._spy = spy if spy is not None else {}

    def run(self, query, parameters=None, **kwargs):
        self._spy["query"] = query
        self._spy["parameters"] = parameters
        self._spy["kwargs"] = kwargs
        return _Result(self._c, self._r)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Session:
    def __init__(self, cols, records, spy=None):
        self._c, self._r = cols, records
        self._spy = spy if spy is not None else {}

    def begin_transaction(self, **config):
        self._spy["tx_config"] = config
        return _Tx(self._c, self._r, self._spy)

    # Kept so a regression back to execute_read() fails loudly rather than
    # quietly losing the transaction timeout.
    def execute_read(self, fn):
        raise AssertionError("query proxy must use an explicit timed transaction")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Neo4j:
    def __init__(self, cols, records, spy=None):
        self._c, self._r = cols, records
        self._spy = spy if spy is not None else {}

    def session(self, **config):
        self._spy["session_config"] = config
        return _Session(self._c, self._r, self._spy)

    def close(self):
        pass


def _client(cols=("n",), records=None, spy=None):
    return make_test_client(neo4j_client=_Neo4j(list(cols), records or [], spy))


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
                    {"c": "http://www.openlinksw.com/schemas/virtrdf#QuadMap"},
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
        assert not any("openlinksw" in cls for cls in b["classes"])  # system IRIs filtered
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


def test_cypher_rejects_admin_procedures():
    """Pentest leak (DAST): `CALL dbms.listConfig()` disclosed Neo4j config.
    dbms.*/apoc.* are now rejected; safe read procedures (db.*) still pass."""
    c = _client()
    try:
        for q in ("CALL dbms.listConfig()", "CALL dbms.components()",
                  "CALL dbms.security.listRoles()",
                  "CALL apoc.load.json('file:///etc/passwd')"):
            assert c.post("/query/cypher", json={"query": q}).status_code == 400, q
        # legit schema-introspection procedure is NOT over-blocked
        assert c.post("/query/cypher", json={"query": "CALL db.labels()"}).status_code == 200
    finally:
        cleanup_dishka()


def test_sql_rejects_filesystem_functions():
    """Pentest CRITICAL (DAST): `pg_read_file('/etc/passwd')` read server files +
    `/proc/1/environ` secrets under the old superuser DSN. The least-privilege
    reader role denies these at the DB; the filter also rejects them up front."""
    c = _client()
    try:
        for q in ("SELECT pg_read_file('/etc/passwd')",
                  "SELECT pg_read_binary_file('/proc/1/environ')",
                  "SELECT pg_ls_dir('.')",
                  "SELECT lo_import('/etc/passwd')"):
            assert c.post("/query/sql", json={"query": q}).status_code == 400, q
    finally:
        cleanup_dishka()


# ── Bind parameters ────────────────────────────────────────────────
# The feed-query catalogue varies one curated query per subscription by
# changing its binds, so these have to be real driver parameters: a value
# must never be able to change the shape of the statement.


def test_cypher_passes_params_to_the_driver():
    spy: dict = {}
    c = _client(cols=("name",), records=[{"name": "A"}], spy=spy)
    try:
        r = c.post("/query/cypher", json={
            "query": "MATCH (co:Company) WHERE co.country IN $nuts RETURN co.name AS name",
            "params": {"nuts": ["PRT", "ESP"], "since": "2026-01-01"},
        })
        assert r.status_code == 200, r.text
        assert spy["parameters"] == {"nuts": ["PRT", "ESP"], "since": "2026-01-01"}
        # Nothing leaks into the driver's own keyword space.
        assert spy["kwargs"] == {}
    finally:
        cleanup_dishka()


def test_cypher_sets_the_transaction_timeout():
    """Regression: the timeout used to be passed to Transaction.run, whose
    signature swallows unknown keywords as Cypher parameters, so no limit was
    applied at all."""
    spy: dict = {}
    c = _client(spy=spy)
    try:
        assert c.post("/query/cypher", json={"query": "MATCH (n) RETURN n"}).status_code == 200
        assert spy["tx_config"]["timeout"] == 8.0
        assert spy["session_config"]["default_access_mode"] == "READ"
        # Absent params must not be forwarded as a stray $timeout bind.
        assert spy["parameters"] == {}
    finally:
        cleanup_dishka()


def test_a_caller_param_cannot_shadow_a_driver_keyword():
    spy: dict = {}
    c = _client(spy=spy)
    try:
        r = c.post("/query/cypher", json={
            "query": "MATCH (n) RETURN n",
            "params": {"timeout": 9999, "parameters": "x"},
        })
        assert r.status_code == 200, r.text
        assert spy["parameters"] == {"timeout": 9999, "parameters": "x"}
        assert spy["tx_config"]["timeout"] == 8.0
    finally:
        cleanup_dishka()


def test_params_reject_bad_names_and_shapes():
    c = _client()
    try:
        bad = [
            {"1nuts": "x"},                     # leading digit
            {"nuts; DROP": "x"},                # punctuation
            {"": "x"},                          # empty
            {"nuts": {"a": 1}},                 # nested object
            {"nuts": [["a"]]},                  # nested list
            {"nuts": [{"a": 1}]},               # list of objects
        ]
        for params in bad:
            r = c.post("/query/cypher", json={"query": "MATCH (n) RETURN n", "params": params})
            assert r.status_code == 400, f"{params} -> {r.status_code}"
        # A non-object params payload is rejected too.
        r = c.post("/query/cypher", json={"query": "MATCH (n) RETURN n", "params": ["a"]})
        assert r.status_code == 400
    finally:
        cleanup_dishka()


def test_params_enforce_count_and_size_caps():
    c = _client()
    try:
        too_many = {f"p{i}": i for i in range(33)}
        r = c.post("/query/cypher", json={"query": "MATCH (n) RETURN n", "params": too_many})
        assert r.status_code == 400
        too_big = {"blob": ["x" * 64 for _ in range(400)]}
        r = c.post("/query/cypher", json={"query": "MATCH (n) RETURN n", "params": too_big})
        assert r.status_code == 400
        too_long = {"many": list(range(513))}
        r = c.post("/query/cypher", json={"query": "MATCH (n) RETURN n", "params": too_long})
        assert r.status_code == 400
    finally:
        cleanup_dishka()


def test_params_accept_scalars_null_and_flat_lists():
    spy: dict = {}
    c = _client(spy=spy)
    try:
        payload = {"s": "a", "i": 1, "f": 1.5, "b": True, "n": None, "l": ["a", 2, None]}
        r = c.post("/query/cypher", json={"query": "MATCH (n) RETURN n", "params": payload})
        assert r.status_code == 200, r.text
        assert spy["parameters"] == payload
    finally:
        cleanup_dishka()


def test_sql_params_are_validated_before_touching_the_db(monkeypatch):
    """A bad params payload is a 400 even when the stats DB is unconfigured,
    proving validation runs ahead of any connection attempt."""
    monkeypatch.delenv("STATS_DATABASE_URL", raising=False)
    c = _client()
    try:
        r = c.post("/query/sql", json={"query": "SELECT 1", "params": {"bad name": 1}})
        assert r.status_code == 400
        # ...while a well-formed payload gets as far as the 503.
        r = c.post("/query/sql", json={"query": "SELECT 1", "params": {"ok": 1}})
        assert r.status_code == 503
    finally:
        cleanup_dishka()


class _Cur:
    """Minimal psycopg cursor double that records what it was executed with."""

    def __init__(self, calls):
        self._calls = calls
        self.description = [type("D", (), {"name": "item_id"})()]

    def execute(self, query, params=None):
        self._calls.append((query, params))

    def fetchmany(self, _n):
        return [["a"]]

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Conn:
    def __init__(self, calls):
        self._calls = calls
        self.read_only = False

    def cursor(self):
        return _Cur(self._calls)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_sql_binds_params_and_omits_them_when_absent(monkeypatch):
    """psycopg only interpolates when a parameter mapping is supplied, so an
    empty params payload must arrive as None — otherwise a literal `%` in an
    unparameterised query would start failing."""
    import psycopg as _psycopg  # pylint: disable=import-outside-toplevel
    from src.api.routers import query as query_router  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("STATS_DATABASE_URL", "postgresql://u@h/db")
    calls: list = []
    monkeypatch.setattr(_psycopg, "connect", lambda *a, **k: _Conn(calls))
    monkeypatch.setattr(query_router.psycopg, "connect", lambda *a, **k: _Conn(calls))

    c = _client()
    try:
        r = c.post("/query/sql", json={
            "query": "SELECT id AS item_id FROM observation WHERE geo_code = ANY(%(nuts)s)",
            "params": {"nuts": ["PT", "ES"]},
        })
        assert r.status_code == 200, r.text
        assert calls[-1][1] == {"nuts": ["PT", "ES"]}

        calls.clear()
        r = c.post("/query/sql", json={"query": "SELECT 1 AS item_id"})
        assert r.status_code == 200, r.text
        assert calls[-1][1] is None
    finally:
        cleanup_dishka()
