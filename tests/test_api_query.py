"""Read-only Data Studio query proxies: /query/cypher + /query/sql."""
# pylint: disable=missing-class-docstring,missing-function-docstring,unused-argument,too-few-public-methods,protected-access
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


def test_unicode_lookalike_param_names_are_rejected():
    """The name pattern is ASCII-only on purpose: a bind name is an
    identifier in someone else's query language, not free text, and a
    Unicode homoglyph must not pass for the ASCII name a query declares."""
    c = _client()
    try:
        # Written as escapes: a literal zero-width space in source is
        # invisible to the next reader (and pylint rejects it outright).
        for name in ("caf\u00e9", "nuts\u200b", "\uff4euts"):
            r = c.post("/query/cypher",
                       json={"query": "MATCH (n) RETURN n", "params": {name: 1}})
            assert r.status_code == 400, f"{name!r} -> {r.status_code}"
    finally:
        cleanup_dishka()


# ── the write/DDL lists are the control, so pin them ─────────────────────
#
# These two tuples ARE the defense-in-depth layer. Both exist because of
# real pentest findings — `CALL dbms.listConfig()` disclosed Neo4j config,
# and `pg_read_file('/etc/passwd')` read server files under the old
# superuser DSN — and the engine-level read-only enforcement behind them is
# the other half, not a replacement.
#
# The existing rejection tests sample the lists: four of the eight Cypher
# keywords, eight of the twenty-four SQL ones. An entry deleted from either
# tuple would leave every one of them passing.
#
# The expected sets below are written out here on purpose rather than
# imported from the module. A test that loops over the module's own tuple
# and asserts each entry is rejected cannot fail when an entry is removed —
# the loop just gets shorter. Holding the list in the test is what makes
# deleting a keyword a failure instead of a silent weakening.

EXPECTED_CYPHER_FORBIDDEN = {
    "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "FOREACH",
}

EXPECTED_SQL_FORBIDDEN = {
    # writes and DDL
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE",
    "GRANT", "REVOKE", "COPY", "MERGE", "VACUUM", "COMMENT", "REINDEX",
    # filesystem / large-object reach (the DAST CRITICAL)
    "PG_READ_FILE", "PG_READ_BINARY_FILE", "PG_LS_DIR", "PG_STAT_FILE",
    "PG_LS_LOGDIR", "PG_LS_WALDIR", "PG_LS_TMPDIR",
    "LO_IMPORT", "LO_EXPORT", "LO_GET",
}


def test_the_forbidden_keyword_lists_are_exactly_these():
    """Removing an entry is a security change and has to be a deliberate
    one — editing this test alongside it."""
    # pylint: disable=import-outside-toplevel
    from src.api.routers import query as query_router
    assert set(query_router._CYPHER_FORBIDDEN) == EXPECTED_CYPHER_FORBIDDEN
    assert set(query_router._SQL_FORBIDDEN) == EXPECTED_SQL_FORBIDDEN


def test_every_forbidden_cypher_keyword_is_rejected_end_to_end():
    """Being in the tuple is not the same as being enforced — the tokeniser
    sits between them."""
    c = _client()
    try:
        for kw in sorted(EXPECTED_CYPHER_FORBIDDEN):
            r = c.post("/query/cypher", json={"query": f"MATCH (n) {kw} n"})
            assert r.status_code == 400, f"{kw} was allowed through"
    finally:
        cleanup_dishka()


def test_every_forbidden_sql_keyword_is_rejected_end_to_end():
    c = _client()
    try:
        for kw in sorted(EXPECTED_SQL_FORBIDDEN):
            r = c.post("/query/sql", json={"query": f"SELECT 1 {kw} x"})
            assert r.status_code == 400, f"{kw} was allowed through"
    finally:
        cleanup_dishka()


def test_the_keyword_filter_is_case_insensitive():
    """A filter a caller escapes by typing lowercase is not a filter. The
    tokeniser upper-cases first; nothing else pins that it does."""
    c = _client()
    try:
        for q in ("MATCH (n) delete n", "MATCH (n) DeLeTe n"):
            assert c.post("/query/cypher", json={"query": q}).status_code == 400, q
        assert c.post(
            "/query/sql",
            json={"query": "select pg_read_file('/etc/passwd')"},
        ).status_code == 400
    finally:
        cleanup_dishka()


def test_punctuation_cannot_hide_a_keyword_from_the_tokeniser():
    """Parens and semicolons are split on before the word check, which is
    what stops `DELETE(n)` reading as one token the list never matches."""
    c = _client()
    try:
        for q in ("MATCH (n) DELETE(n)", "MATCH (n) RETURN n;DROP x"):
            assert c.post("/query/cypher", json={"query": q}).status_code == 400, q
    finally:
        cleanup_dishka()


def test_the_filter_does_not_over_block_ordinary_reads():
    """The other failure direction: a read-only studio that refuses reads.
    Words merely CONTAINING a keyword must pass — `created_at` is a real
    column name and `MERGE` living inside `MERGED` is not a write."""
    c = _client()
    try:
        for q in ("MATCH (n) RETURN n.created_at",
                  "MATCH (n:Merged) RETURN n",
                  "MATCH (n) RETURN n LIMIT 10"):
            assert c.post("/query/cypher", json={"query": q}).status_code == 200, q
    finally:
        cleanup_dishka()


# ── Neo4j notifications: telling a typo from an empty answer ─────────────
#
# `MATCH (c:Compnay) RETURN c` is valid Cypher. It parses, plans, runs, and
# matches nothing — forever. Neo4j says so on the result summary, and the
# proxy forwards those notifications so the Studio editor and the
# assistant's pre-save check can tell a misspelled label from an honestly
# empty result. SQL needs no equivalent: it rejects an unknown column
# outright.
#
# None of it was exercised. The shared _Result above has no `consume()`, so
# every existing test lands in the best-effort `except` and gets [] — which
# is also what a working extraction returns for a clean query, so the
# feature could be entirely broken and the suite would not move.
#
# The driver has changed this API across versions, which is why the code
# reads it four ways: `summary_notifications` then `notifications`, and
# per-item either mapping keys or attributes, with severity under two
# different names. Each shape gets a test because each is a real driver.

class _Summary:
    def __init__(self, summary_notifications=None, notifications=None):
        if summary_notifications is not None:
            self.summary_notifications = summary_notifications
        if notifications is not None:
            self.notifications = notifications


class _NoteResult:
    """A result that can be drained and then asked for its summary."""

    def __init__(self, cols, records, summary):
        self._cols, self._records, self._summary = cols, records, summary

    def keys(self):
        return self._cols

    def __iter__(self):
        return iter(self._records)

    def consume(self):
        return self._summary


class _NoteTx:
    def __init__(self, result):
        self._result = result

    def run(self, query, parameters=None, **kwargs):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _NoteSession:
    def __init__(self, result):
        self._result = result

    def begin_transaction(self, **config):
        return _NoteTx(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _NoteNeo4j:
    def __init__(self, result):
        self._result = result

    def session(self, **config):
        return _NoteSession(self._result)

    def close(self):
        pass


def _notes_client(summary, cols=("c",), records=()):
    return make_test_client(
        neo4j_client=_NoteNeo4j(_NoteResult(list(cols), list(records), summary)))


class _ObjNote:
    """A driver that hands back objects rather than mappings."""

    def __init__(self, code, title, description, severity_level):
        self.code = code
        self.title = title
        self.description = description
        self.severity_level = severity_level


def test_a_mapping_notification_reaches_the_caller():
    """The typo case, end to end: no rows, but a warning explaining why."""
    summary = _Summary(summary_notifications=[{
        "code": "Neo.ClientNotification.Statement.UnknownLabelWarning",
        "title": "The provided label is not in the database.",
        "description": "One of the labels does not exist: (:Compnay)",
        "severity": "WARNING",
    }])
    c = _notes_client(summary)
    try:
        r = c.post("/query/cypher", json={"query": "MATCH (c:Compnay) RETURN c"})
        assert r.status_code == 200
        body = r.json()
        assert body["rows"] == []
        assert len(body["notifications"]) == 1
        note = body["notifications"][0]
        assert note["code"].endswith("UnknownLabelWarning")
        assert "not in the database" in note["title"]
        assert "Compnay" in note["description"]
        assert note["severity"] == "WARNING"
    finally:
        cleanup_dishka()


def test_an_object_notification_reaches_the_caller_too():
    """Newer drivers return objects with `severity_level`, not mappings.
    Reading only one shape means warnings vanish on a driver bump — silently,
    since an empty list is indistinguishable from a clean query."""
    summary = _Summary(summary_notifications=[
        _ObjNote("Neo.ClientNotification.Statement.UnknownLabelWarning",
                 "Unknown label", "One of the labels does not exist", "WARNING"),
    ])
    c = _notes_client(summary)
    try:
        note = c.post("/query/cypher",
                      json={"query": "MATCH (c:Compnay) RETURN c"}).json()["notifications"][0]
        assert note["code"].endswith("UnknownLabelWarning")
        assert note["title"] == "Unknown label"
        assert note["severity"] == "WARNING"
    finally:
        cleanup_dishka()


def test_the_older_notifications_attribute_is_still_read():
    """`summary_notifications` is the newer name; a driver offering only
    `notifications` must still have its warnings forwarded."""
    summary = _Summary(notifications=[
        {"code": "X", "title": "T", "description": "D", "severity": "INFORMATION"},
    ])
    c = _notes_client(summary)
    try:
        notes = c.post("/query/cypher",
                       json={"query": "MATCH (n) RETURN n"}).json()["notifications"]
        assert [n["code"] for n in notes] == ["X"]
        assert notes[0]["severity"] == "INFORMATION"
    finally:
        cleanup_dishka()


def test_a_driver_without_notifications_does_not_take_the_rows_down():
    """Best-effort, and it has to stay that way: notifications are a
    convenience, the rows are the answer."""
    c = _notes_client(_Summary(), cols=("n",), records=({"n": 1},))
    try:
        r = c.post("/query/cypher", json={"query": "MATCH (n) RETURN n"})
        assert r.status_code == 200
        assert r.json()["rows"] == [[1]]
        assert r.json()["notifications"] == []
    finally:
        cleanup_dishka()


def test_a_long_description_is_truncated_before_it_reaches_the_editor():
    """The panel renders these; an unbounded engine string does not belong
    in a response the Studio puts on screen."""
    summary = _Summary(summary_notifications=[
        {"code": "X", "title": "T", "description": "d" * 900, "severity": "WARNING"},
    ])
    c = _notes_client(summary)
    try:
        note = c.post("/query/cypher",
                      json={"query": "MATCH (n) RETURN n"}).json()["notifications"][0]
        assert len(note["description"]) == 300
    finally:
        cleanup_dishka()


def test_a_notification_missing_every_field_is_still_a_string_shape():
    """Whatever the driver omits, the response keeps four string fields —
    the editor renders them directly and a null there is a crash."""
    summary = _Summary(summary_notifications=[{}])
    c = _notes_client(summary)
    try:
        note = c.post("/query/cypher",
                      json={"query": "MATCH (n) RETURN n"}).json()["notifications"][0]
        assert note == {"code": "", "title": "", "description": "", "severity": ""}
    finally:
        cleanup_dishka()
