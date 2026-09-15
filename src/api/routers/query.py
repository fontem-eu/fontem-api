"""Read-only query proxies.

Cypher (Neo4j) and SQL (stats Postgres). SPARQL keeps its own /sparql router.
All three are strictly read-only, size- and row-capped: callers explore and plot
the graph/stores, never mutate them.

Read-only is enforced at the engine (Neo4j read transaction / Postgres read-only
transaction) AND by a write-keyword allow-list as defense-in-depth, mirroring the
SPARQL proxy.

BIND PARAMETERS. Both endpoints accept an optional ``params`` object alongside
``query``. Values are handed to the driver as real binds — psycopg's
``%(name)s`` for SQL, ``$name`` for Cypher — never spliced into the query text,
so a parameter can never change the shape of the statement. Placeholder syntax
stays engine-native: we do not rewrite the query, because rewriting is exactly
the step that reintroduces injection.

This exists for the feed-query catalogue, where one curated query serves many
subscriptions by varying its binds (region, watermark) rather than being forked
per subscriber. The Data Studio benefits too.
"""
from __future__ import annotations

from collections import defaultdict
import datetime as _dt
import decimal
import json
import re
import logging
import os
import threading
import time
from typing import Annotated

import psycopg
from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Body, HTTPException
from neo4j import READ_ACCESS
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from src.data.graph.neo4j_client import Neo4jClient
from src.data.sparql.virtuoso_client import VirtuosoClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/query", tags=["query"])

_MAX_QUERY_BYTES = 8192
_ROW_CAP = 1000
_TIMEOUT_MS = 8000

# Write/DDL keywords rejected up front (the engine enforces read-only too).
_CYPHER_FORBIDDEN = ("CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "FOREACH")
_SQL_FORBIDDEN = (
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE",
    "GRANT", "REVOKE", "COPY", "MERGE", "VACUUM", "COMMENT", "REINDEX",
    # filesystem / program / large-object functions — the read-only reader
    # role already rejects these, but block them up front too (defense in depth):
    "PG_READ_FILE", "PG_READ_BINARY_FILE", "PG_LS_DIR", "PG_STAT_FILE",
    "PG_LS_LOGDIR", "PG_LS_WALDIR", "PG_LS_TMPDIR", "LO_IMPORT", "LO_EXPORT", "LO_GET",
)

# Neo4j admin / file-access procedures — a reader-role Neo4j user can't call
# them, but block by name too so it holds on Community (no RBAC). Safe read
# procedures (db.labels / db.relationshipTypes for schema) keep the `db.` prefix.
# gds.* too: prod loads Graph Data Science, whose export procedures write files.
_CYPHER_PROC_DENY = re.compile(r"\b(dbms|apoc|gds)\s*\.", re.IGNORECASE)

# Clauses that reach OUTSIDE reading the graph. Unlike writes, the read
# transaction does not stop these — fetching a URL is not a write — so for
# them this check is the control, not a second layer.
#
# `LOAD CSV FROM 'file:///…'` reads the server's disk; `FROM 'http://…'` makes
# Neo4j request any address it can reach, cluster-internal services included,
# and hands the body back as rows. PERIODIC COMMIT and IN TRANSACTIONS exist
# to batch exactly that import (anchored on the subquery's closing brace, so
# a variable called `transactions` is not mistaken for one). TERMINATE stops
# other callers' queries.
_CYPHER_REACH_DENY = re.compile(
    r"\bLOAD\s+CSV\b"
    r"|\bPERIODIC\s+COMMIT\b"
    r"|\}\s*IN\s+(?:[$\w]+\s+)?(?:CONCURRENT\s+)?TRANSACTIONS\b"
    r"|(?<![.\w$])TERMINATE\b",
    re.IGNORECASE,
)

# SHOW is for schema listings only. SHOW SETTINGS discloses the same server
# configuration dbms.listConfig() did, and SHOW TRANSACTIONS the text and
# parameters of other callers' queries. An allow-list, because Neo4j keeps
# adding SHOW commands and a deny-list would not hear about them.
_CYPHER_SHOW = re.compile(r"(?<![.\w$])SHOW\b", re.IGNORECASE)
_CYPHER_SHOW_ALLOWED = re.compile(
    r"SHOW\s+"
    r"(?:(?:ALL|BUILT|IN|USER|DEFINED|RANGE|FULLTEXT|TEXT|POINT|VECTOR|LOOKUP|"
    r"NODE|RELATIONSHIP|REL|UNIQUE|UNIQUENESS|EXIST|EXISTENCE|KEY|PROPERTY|PROP|TYPE)\s+)*"
    r"(?:INDEX|INDEXES|CONSTRAINT|CONSTRAINTS|PROCEDURE|PROCEDURES|FUNCTION|FUNCTIONS)\b",
    re.IGNORECASE,
)


#: Line-comment markers per language. `#` is deliberately absent: it starts
#: a comment in MySQL and SPARQL but not in Postgres or Cypher, and treating
#: it as one here would blind the scan to text those engines DO execute.
_LINE_COMMENT = {"Cypher": ("//",), "SQL": ("--",)}


def _literal_end(query: str, start: int, lang: str) -> int:
    r"""Index of the quote closing the literal opened at ``start``, or -1.

    Cypher strings take backslash escapes, so ``'O\'Brien'`` is ONE string.
    Ending it at the escaped quote ends it early for this scan but not for the
    engine, and the stretch up to the next quote is then blanked as "string"
    while Neo4j executes it:

        WITH 'a\'b' AS x LOAD CSV FROM 'file:///etc/passwd' AS r RETURN r

    Postgres standard strings have no backslash escape, so SQL keeps the plain
    scan — honouring one there would open the same gap from the other side.
    Backtick identifiers escape by doubling, never by backslash.
    """
    quote = query[start]
    escapes = lang == "Cypher" and quote != "`"
    j = start + 1
    while j < len(query):
        if escapes and query[j] == "\\":
            j += 2
            continue
        if query[j] == quote:
            return j
        j += 1
    return -1


def _strip_noncode(query: str, lang: str, keep_identifiers: bool = False) -> str:
    """The query with comments and string literals blanked out.

    For SCANNING only — the engine is always sent the original. A keyword
    inside a comment or a quoted string is not a statement, and treating it
    as one rejected working queries: a model wrote

        // Same set of contracts, grouped by what was bought
        MATCH (c:Contract)-[:AWARDED_TO]->(co:Company) ...

    and was refused for "the write/DDL keyword 'SET'", because upper-casing
    and splitting turns the English word "set" into the Cypher clause. The
    query was read-only. Documenting it was what made it look dangerous.

    Blanked, not deleted, so nothing on either side of a removed span is
    joined into a new token that was never written.

    An UNTERMINATED string or block comment is left in place. Stripping to
    the end of the input on an unclosed quote is exactly how a keyword after
    it would be hidden from this scan; such a query cannot parse anyway, so
    the conservative reading costs nothing real.

    ``keep_identifiers`` blanks only the backticks of a quoted identifier and
    keeps its name, for checks that match on names rather than clauses.
    """
    line_markers = _LINE_COMMENT.get(lang, ())
    out = list(query)
    i, n = 0, len(query)
    while i < n:
        ch = query[i]
        # Block comment — both languages use /* ... */
        if query.startswith("/*", i):
            end = query.find("*/", i + 2)
            if end == -1:
                break                      # unterminated: leave the rest alone
            for j in range(i, end + 2):
                out[j] = " "
            i = end + 2
            continue
        marker = next((m for m in line_markers if query.startswith(m, i)), None)
        if marker:
            end = query.find("\n", i)
            end = n if end == -1 else end
            for j in range(i, end):
                out[j] = " "
            i = end
            continue
        if ch in ("'", '"', "`"):
            end = _literal_end(query, i, lang)
            if end == -1:
                break                      # unterminated: leave the rest alone
            if ch == "`" and keep_identifiers:
                out[i] = out[end] = " "
            else:
                for j in range(i, end + 1):
                    out[j] = " "
            i = end + 1
            continue
        i += 1
    return "".join(out)


def _validate(query: str, forbidden: tuple, lang: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="Body must include a non-empty `query` string")
    if len(query.encode("utf-8")) > _MAX_QUERY_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Query exceeds the {_MAX_QUERY_BYTES}-byte studio limit",
        )
    # Scanned with comments and string literals blanked; the engine still
    # receives the query exactly as written.
    scannable = _strip_noncode(query, lang)
    words = set(scannable.upper().replace("(", " ").replace(")", " ").replace(";", " ").split())
    hit = next((t for t in forbidden if t in words), None)
    if hit:
        raise HTTPException(
            status_code=400,
            detail=f"{lang}: write/DDL keyword '{hit}' is not allowed (read-only studio).",
        )
    return query.strip()


def _reject_cypher_reach(query: str) -> None:
    """Refuse Cypher that does more than read the graph it is pointed at.

    Scanned like the keyword check, comments and strings blanked, except that
    backtick-quoted identifiers keep their names: ``CALL `dbms`.listConfig()``
    calls the same procedure as ``CALL dbms.listConfig()``, and the backtick
    between the name and the dot hid it from a check on the raw text.
    """
    scannable = _strip_noncode(query, "Cypher", keep_identifiers=True)
    if _CYPHER_PROC_DENY.search(scannable):
        raise HTTPException(
            status_code=400,
            detail="Cypher: dbms.*/apoc.*/gds.* procedures are not allowed (read-only studio).",
        )
    reach = _CYPHER_REACH_DENY.search(scannable)
    if reach:
        clause = " ".join(reach.group(0).lstrip("}").split()).upper()
        raise HTTPException(
            status_code=400,
            detail=f"Cypher: '{clause}' is not allowed (read-only studio).",
        )
    for show in _CYPHER_SHOW.finditer(scannable):
        if not _CYPHER_SHOW_ALLOWED.match(scannable, show.start()):
            raise HTTPException(
                status_code=400,
                detail="Cypher: only SHOW INDEXES / CONSTRAINTS / PROCEDURES / FUNCTIONS "
                       "are allowed (read-only studio).",
            )


# ── Bind parameters ─────────────────────────────────────────────────
# Deliberately narrow. A bind carries a VALUE, so scalars and flat lists of
# scalars are the whole vocabulary — a list covers the region filter
# (`geo_code = ANY(%(nuts)s)` / `IN $nuts`), which is the case that motivated
# this. Nested structures are rejected rather than flattened: silently
# reshaping a caller's input is how surprises get built.
_MAX_PARAMS = 32
_MAX_PARAM_ITEMS = 512
_MAX_PARAM_BYTES = 16384
# re.ASCII matters: without it \w also matches Unicode letters, and a bind
# name is an identifier in someone else's query language, not free text.
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_]\w{0,63}$", re.ASCII)
_PARAM_SCALARS = (str, int, float, bool)


def _validate_param_value(name: str, value):
    """Accept a scalar, None, or a flat list of those. Reject anything else."""
    if value is None or isinstance(value, _PARAM_SCALARS):
        return value
    if isinstance(value, list):
        if len(value) > _MAX_PARAM_ITEMS:
            raise HTTPException(
                status_code=400,
                detail=f"Parameter '{name}' exceeds {_MAX_PARAM_ITEMS} items",
            )
        for item in value:
            if item is not None and not isinstance(item, _PARAM_SCALARS):
                raise HTTPException(
                    status_code=400,
                    detail=f"Parameter '{name}' may only contain strings, numbers, "
                           "booleans or null",
                )
        return value
    raise HTTPException(
        status_code=400,
        detail=f"Parameter '{name}' must be a string, number, boolean, null, or a "
               "flat list of those",
    )


def _validate_params(raw) -> dict:
    """Validate the optional ``params`` payload into a driver-ready dict.

    Returns {} when absent, which callers treat as "pass no parameters at all"
    — not "pass an empty mapping" — because psycopg changes its handling of a
    literal ``%`` the moment any parameter mapping is supplied.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400,
            detail="`params` must be an object mapping parameter names to values",
        )
    if len(raw) > _MAX_PARAMS:
        raise HTTPException(
            status_code=400,
            detail=f"At most {_MAX_PARAMS} parameters are allowed",
        )
    out: dict = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _PARAM_NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid parameter name {name!r}: must start with a letter or "
                       "underscore and contain only letters, digits and underscores",
            )
        out[name] = _validate_param_value(name, value)
    encoded = len(json.dumps(out, default=str).encode("utf-8"))
    if encoded > _MAX_PARAM_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Parameters exceed the {_MAX_PARAM_BYTES}-byte limit",
        )
    return out


def _notifications(result) -> list[dict]:
    """What Neo4j warns about, alongside what it returns.

    A misspelled label is valid Cypher: `MATCH (c:Compnay)` parses, plans,
    runs, and matches nothing — forever. The engine says so, as a
    notification on the result summary (UnknownLabelWarning and friends),
    and this endpoint used to throw them away. Nothing downstream could
    then tell a typo from an honestly empty answer: not the Studio editor,
    and not the assistant's check on a query before it saves one.

    That is the difference from SQL, which rejects an unknown table or
    column outright and needs no equivalent.

    Best-effort: notifications are a driver convenience, and a driver that
    does not offer them must not take the query results down with it.
    """
    try:
        notes = result.consume().summary_notifications
    except Exception:  # pylint: disable=broad-exception-caught
        try:
            notes = result.consume().notifications or []
        except Exception:  # pylint: disable=broad-exception-caught
            return []
    out = []
    for n in notes or []:
        if isinstance(n, dict):
            code, title = n.get("code"), n.get("title")
            desc = n.get("description")
            severity = n.get("severity") or n.get("severity_level")
        else:
            code = getattr(n, "code", None)
            title = getattr(n, "title", None)
            desc = getattr(n, "description", None)
            severity = (getattr(n, "severity_level", None)
                        or getattr(n, "severity", None))
        out.append({
            "code": str(code or ""),
            "title": str(title or ""),
            "description": str(desc or "")[:300],
            "severity": str(severity or ""),
        })
    return out


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.date, _dt.datetime, _dt.time)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


@router.post(
    "/cypher",
    responses={
        400: {"description": "invalid / forbidden query"},
        503: {"description": "store unavailable"},
        504: {"description": "timeout"},
    },
)
@inject
def cypher_query(body: Annotated[dict, Body(...)], neo4j: FromDishka[Neo4jClient]) -> dict:
    """Run a read-only Cypher query against Neo4j. Returns { columns, rows }.

    Optional ``params`` are bound as Cypher ``$name`` parameters.
    """
    body = body or {}
    query = _validate(body.get("query") or "", _CYPHER_FORBIDDEN, "Cypher")
    params = _validate_params(body.get("params"))
    _reject_cypher_reach(query)

    def _run(tx):
        # `parameters=` (not **kwargs) so a caller-supplied bind can never
        # collide with a driver keyword.
        res = tx.run(query, parameters=params)
        cols = list(res.keys())
        out = []
        truncated = False
        for i, rec in enumerate(res):
            if i >= _ROW_CAP:
                truncated = True
                break
            out.append([_jsonable(rec[c]) for c in cols])
        # `break`, not an early return: the summary is only complete once
        # the result is drained, and the notifications come off it below.
        return cols, out, truncated, _notifications(res)

    try:
        # An explicit transaction, because the timeout has to be set when the
        # transaction BEGINS. The previous `tx.run(query, timeout=...)` did
        # nothing of the sort: Transaction.run's signature is
        # (query, parameters=None, **kwparameters), so `timeout` was silently
        # accepted as a Cypher parameter named $timeout and no limit was ever
        # applied. READ_ACCESS is kept on the session — the server rejects
        # writes inside a read transaction — and the transaction is never
        # committed, so it rolls back on exit either way.
        with neo4j.session(default_access_mode=READ_ACCESS) as session:
            with session.begin_transaction(timeout=_TIMEOUT_MS / 1000) as tx:
                cols, rows, truncated, notes = _run(tx)
    except HTTPException:
        raise
    except (ServiceUnavailable, SessionExpired) as exc:
        # The store is down or the connection dropped -- nothing to do with
        # the query. This used to fall into the branch below and answer 400,
        # which tells the caller their query is invalid when the database
        # was simply unreachable.
        #
        # That is not cosmetic. The feed catalogue re-validates published
        # queries and DEMOTES any that stop validating, so one Neo4j restart
        # silently un-published three briefings and emptied the public feed
        # -- a state that did not heal when Neo4j came back.
        raise HTTPException(
            status_code=503,
            detail=f"Cypher store unavailable: {str(exc)[:300]}",
        ) from exc
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # surface engine/driver errors to the editor rather than 500-ing
        msg = str(exc)
        code = 504 if "timeout" in msg.lower() else 400
        raise HTTPException(status_code=code, detail=f"Cypher error: {msg[:300]}") from exc
    return {"columns": cols, "rows": rows, "row_count": len(rows),
            "truncated": truncated, "notifications": notes}


def _stats_dsn() -> str | None:
    dsn = os.environ.get("STATS_DATABASE_URL")
    if not dsn:
        return None
    return (dsn.replace("postgresql+asyncpg://", "postgresql://")
            .replace("postgresql+psycopg://", "postgresql://"))


@router.post(
    "/sql",
    responses={
        400: {"description": "invalid / forbidden query"},
        503: {"description": "stats DB unset"},
        504: {"description": "timeout"},
    },
)
def sql_query(body: Annotated[dict, Body(...)]) -> dict:
    """Run a read-only SQL query against the stats Postgres. Returns { columns, rows }.

    Optional ``params`` are bound as psycopg ``%(name)s`` placeholders. Note
    that supplying any parameter puts psycopg into interpolation mode, so a
    literal percent sign in such a query must be written ``%%``.
    """
    body = body or {}
    query = _validate(body.get("query") or "", _SQL_FORBIDDEN, "SQL")
    params = _validate_params(body.get("params"))
    dsn = _stats_dsn()
    if not dsn:
        raise HTTPException(
            status_code=503,
            detail="SQL studio not configured (STATS_DATABASE_URL unset)",
        )
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.read_only = True  # engine-enforced: any write raises
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {_TIMEOUT_MS}")
                # `or None`, never `params` — an empty dict would still switch
                # psycopg into interpolation mode and break a bare `%`.
                cur.execute(query, params or None)
                cols = [d.name for d in cur.description] if cur.description else []
                rows = [[_jsonable(v) for v in r] for r in cur.fetchmany(_ROW_CAP)]
                truncated = cur.fetchone() is not None
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=504, detail="SQL query exceeded the timeout") from exc
    except psycopg.Error as exc:
        raise HTTPException(status_code=400, detail=f"SQL error: {str(exc)[:300]}") from exc
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated}


# ── Schema introspection ────────────────────────────────────────────
# Powers the editor's syntax-aware autocomplete + a browsable schema panel.
# Read-only metadata; cached because it changes rarely and some stores
# (Virtuoso) are expensive to introspect.
_SCHEMA_CACHE: dict = {}
_SCHEMA_TTL = 600  # seconds
# One lock per store. The Studio asks for the schema from several places as a
# page opens, so an expired entry used to be rebuilt by every concurrent
# caller at once; now the first builds it and the rest wait for that result.
_SCHEMA_LOCKS: dict = defaultdict(threading.Lock)


def _cached(key, builder):
    hit = _SCHEMA_CACHE.get(key)
    if hit and time.time() - hit[0] < _SCHEMA_TTL:
        return hit[1]
    with _SCHEMA_LOCKS[key]:
        hit = _SCHEMA_CACHE.get(key)
        if hit and time.time() - hit[0] < _SCHEMA_TTL:
            return hit[1]
        payload = builder()
        _SCHEMA_CACHE[key] = (time.time(), payload)
        return payload


#: Nodes read per label to list that label's properties for autocomplete.
_SCHEMA_SAMPLE = 200


def _cypher_schema(neo4j: Neo4jClient) -> dict:
    def _labels(tx):
        return [r["label"] for r in
                tx.run("CALL db.labels() YIELD label RETURN label ORDER BY label")]

    def _rels(tx):
        return [r["relationshipType"] for r in
                tx.run("CALL db.relationshipTypes() YIELD relationshipType "
                       "RETURN relationshipType ORDER BY relationshipType")]

    def _node_props(tx, labels):
        # Sampled, not db.schema.nodeTypeProperties(): that procedure scans
        # every node in the store, which took 125-150 s per call on the shared
        # graph and held Neo4j busy while the Studio's own query ran. A few
        # hundred nodes per label is enough to autocomplete with; the complete
        # key list still comes from db.propertyKeys() below.
        out: dict = {}
        for lbl in labels:
            name = lbl.replace("`", "``")
            out[lbl] = [r["k"] for r in tx.run(
                f"MATCH (n:`{name}`) WITH n LIMIT {_SCHEMA_SAMPLE} "
                "UNWIND keys(n) AS k RETURN DISTINCT k ORDER BY k")]
        return {k: v for k, v in out.items() if v}

    def _keys(tx):
        return [r["propertyKey"] for r in tx.run(
                "CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey ORDER BY propertyKey")]

    with neo4j.session() as session:
        labels = session.execute_read(_labels)
        rels = session.execute_read(_rels)
        label_props = session.execute_read(_node_props, labels)
        props = session.execute_read(_keys)
    return {"lang": "cypher", "labels": labels, "relationshipTypes": rels,
            "labelProperties": label_props, "properties": props}


def _sql_schema() -> dict:
    dsn = _stats_dsn()
    if not dsn:
        raise HTTPException(status_code=503,
                            detail="SQL studio not configured (STATS_DATABASE_URL unset)")
    tables: dict = {}
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {_TIMEOUT_MS}")
            cur.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name !~ '^(pg_|hypopg_)' "  # drop pg_qualstats/hypopg extension views
                "ORDER BY table_name, ordinal_position"
            )
            for tname, cname, dtype in cur.fetchall():
                tables.setdefault(tname, []).append({"name": cname, "type": dtype})
    return {"lang": "sql", "tables": [{"name": t, "columns": c} for t, c in sorted(tables.items())]}


def _sparql_term(val):
    # VirtuosoClient bindings map var -> value (URI/literal string, or an
    # envelope dict). Reduce to the bare IRI/literal string.
    if isinstance(val, dict):
        return val.get("value", "")
    return val


# Virtuoso's own metadata leaks into { ?s ?p ?o } — filter those RDF namespace
# IRIs out by host substring (avoids embedding an http:// scheme literal; these
# are identifiers, never fetched).
_SPARQL_SYSTEM_HOSTS = ("openlinksw.com/", "w3.org/ns/sparql-service-description")


def _is_system_iri(iri: str) -> bool:
    return any(host in iri for host in _SPARQL_SYSTEM_HOSTS)


def _sparql_schema(virtuoso: VirtuosoClient | None) -> dict:
    if virtuoso is None:
        return {"lang": "sparql", "classes": [], "predicates": []}
    classes: list = []
    predicates: list = []
    try:
        classes = [_sparql_term(b.get("c")) for b in
                   virtuoso.query("SELECT DISTINCT ?c WHERE { ?s a ?c } LIMIT 300")]
        predicates = [_sparql_term(b.get("p")) for b in
                      virtuoso.query("SELECT DISTINCT ?p WHERE { ?s ?p ?o } LIMIT 500")]
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # introspection is best-effort; degrade to keyword-only autocomplete
    return {"lang": "sparql",
            "classes": [c for c in classes if c and not _is_system_iri(c)],
            "predicates": [p for p in predicates if p and not _is_system_iri(p)]}


@router.get(
    "/schema/{lang}",
    responses={400: {"description": "unknown language"}, 503: {"description": "store unset"}},
)
@inject
def query_schema(
    lang: str,
    neo4j: FromDishka[Neo4jClient],
    virtuoso: FromDishka[VirtuosoClient | None],
) -> dict:
    """Read-only schema of a store for editor autocomplete + browsing."""
    if lang == "cypher":
        return _cached("cypher", lambda: _cypher_schema(neo4j))
    if lang == "sql":
        return _cached("sql", _sql_schema)
    if lang == "sparql":
        return _cached("sparql", lambda: _sparql_schema(virtuoso))
    raise HTTPException(status_code=400, detail=f"Unknown query language '{lang}'")
