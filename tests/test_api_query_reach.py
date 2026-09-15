"""/query/cypher: clauses and procedures that reach past reading the graph."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

from tests.dishka_fixtures import cleanup_dishka
from tests.test_api_query import _client


# ── reaching past the graph ──────────────────────────────────────────────
#
# Writes have two layers: this filter and the engine's read transaction.
# What follows has ONE. Fetching a URL or reading server config is not a
# write, so Neo4j runs it inside a read transaction without complaint, and
# every spelling that reaches the engine has to be refused here.


def test_cypher_rejects_load_csv_in_every_spelling():
    """`LOAD CSV FROM 'file:///…'` reads the server's disk; `FROM 'http://…'`
    makes Neo4j request any address it can reach inside the cluster and
    returns the body as rows. Comments and a closing brace are all the
    separation the Cypher lexer needs, so they are all tried."""
    c = _client()
    try:
        for q in (
            "LOAD CSV FROM 'file:///etc/passwd' AS r RETURN r",
            "load csv with headers from 'http://vault.vault:8200/v1/sys/health' AS r RETURN r",
            "MATCH (n) WITH n LIMIT 1 LOAD\n  CSV FROM 'http://x' AS r RETURN r",
            "LOAD /* split */ CSV FROM 'http://x' AS r RETURN r",
            "LOAD // split\nCSV FROM 'http://x' AS r RETURN r",
            "CALL { RETURN 1 AS one }LOAD CSV FROM 'http://x' AS r RETURN r",
            "USING PERIODIC COMMIT 500 LOAD CSV FROM 'http://x' AS r RETURN r",
            "MATCH (n) CALL { WITH n RETURN n.x AS x } IN TRANSACTIONS OF 10 ROWS RETURN x",
            "MATCH (n) CALL { WITH n RETURN n.x AS x } IN 4 CONCURRENT TRANSACTIONS RETURN x",
        ):
            assert c.post("/query/cypher", json={"query": q}).status_code == 400, q
    finally:
        cleanup_dishka()


def test_string_escapes_cannot_hide_code_from_the_scanner():
    """Cypher strings take backslash escapes. A scanner that ends `'a\'b'`
    at the escaped quote blanks the next stretch of real code as if it were
    string content — here the clause the engine then runs."""
    c = _client()
    try:
        for q in (
            r"WITH 'a\'b' AS x LOAD CSV FROM 'file:///etc/passwd' AS r RETURN r",
            r'WITH "a\"b" AS x LOAD CSV FROM "file:///etc/passwd" AS r RETURN r',
            r"MATCH (n) WITH n, 'a\'b' AS x DETACH DELETE n RETURN 1 //'",
        ):
            assert c.post("/query/cypher", json={"query": q}).status_code == 400, q
        # an escaped quote in an ordinary read is still an ordinary read
        assert c.post(
            "/query/cypher",
            json={"query": r"MATCH (c:Company) WHERE c.name = 'O\'Brien' RETURN c"},
        ).status_code == 200
        # and Postgres has no backslash escape: honouring one in SQL would hide
        # the function call after `'\'` instead
        assert c.post(
            "/query/sql",
            json={"query": "SELECT '\\' AS a, pg_read_file('/etc/passwd') AS b"},
        ).status_code == 400
    finally:
        cleanup_dishka()


def test_backticks_and_comments_cannot_hide_a_procedure_namespace():
    """The procedure check used to look for `dbms.` in the raw text, and
    `` `dbms`.listConfig() `` has a backtick between the name and the dot."""
    c = _client()
    try:
        for q in (
            "CALL `dbms`.listConfig()",
            "CALL `dbms`.`listConfig`()",
            "CALL `dbms.listConfig`()",
            "CALL `apoc`.load.json('file:///etc/passwd')",
            "CALL dbms/* x */.listConfig()",
            "CALL gds.graph.export('g', {dbName: 'x'})",
            "RETURN `apoc`.text.clean('x')",
        ):
            assert c.post("/query/cypher", json={"query": q}).status_code == 400, q
        # the namespace appearing as data is not a call
        assert c.post(
            "/query/cypher",
            json={"query": "MATCH (d:Document) WHERE d.title CONTAINS 'dbms.listConfig' "
                           "RETURN d"},
        ).status_code == 200
    finally:
        cleanup_dishka()


def test_show_is_limited_to_schema_listings():
    """SHOW SETTINGS is dbms.listConfig() under another name, and SHOW
    TRANSACTIONS lists other callers' queries with their parameters."""
    c = _client()
    try:
        for q in (
            "SHOW SETTINGS", "show settings yield name, value",
            "SHOW SETTINGS YIELD name AS indexes",
            "SHOW TRANSACTIONS", "SHOW USERS", "SHOW CURRENT USER",
            "SHOW USER PRIVILEGES", "SHOW SERVERS", "SHOW DATABASES",
            "TERMINATE TRANSACTIONS 'neo4j-transaction-1'",
        ):
            assert c.post("/query/cypher", json={"query": q}).status_code == 400, q
        for q in (
            "SHOW INDEXES", "SHOW RANGE INDEXES", "SHOW ALL CONSTRAINTS",
            "SHOW UNIQUENESS CONSTRAINTS", "SHOW PROCEDURES",
            "SHOW USER DEFINED FUNCTIONS",
        ):
            assert c.post("/query/cypher", json={"query": q}).status_code == 200, q
    finally:
        cleanup_dishka()


def test_the_reach_checks_do_not_over_block_ordinary_reads():
    """The words alone are not the clauses: a property, a variable, or the
    text of a document may be called any of them."""
    c = _client()
    try:
        for q in (
            "MATCH (d:Document) WHERE d.body CONTAINS 'LOAD CSV' RETURN d",
            "MATCH (n) RETURN n.load AS load",
            "WITH ['a'] AS transactions MATCH (t:Tx) WHERE t.kind IN transactions RETURN t",
            "MATCH (e:Event) RETURN e.show AS s, e.terminate AS t",
            "MATCH (n:`Has Space`) RETURN n",
        ):
            assert c.post("/query/cypher", json={"query": q}).status_code == 200, q
    finally:
        cleanup_dishka()
