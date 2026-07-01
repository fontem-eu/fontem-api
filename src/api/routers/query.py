"""Read-only query proxies for the Data Studio.

Cypher (Neo4j) and SQL (stats Postgres). SPARQL keeps its own /sparql router.
All three are strictly read-only, size- and row-capped: the studio lets users
explore and plot the graph/stores, never mutate them.

Read-only is enforced at the engine (Neo4j read transaction / Postgres read-only
transaction) AND by a write-keyword allow-list as defense-in-depth, mirroring the
SPARQL proxy.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import logging
import os
from typing import Annotated

import psycopg
from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Body, HTTPException

from src.data.graph.neo4j_client import Neo4jClient

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
)


def _validate(query: str, forbidden: tuple, lang: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="Body must include a non-empty `query` string")
    if len(query.encode("utf-8")) > _MAX_QUERY_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Query exceeds the {_MAX_QUERY_BYTES}-byte studio limit",
        )
    words = set(query.upper().replace("(", " ").replace(")", " ").replace(";", " ").split())
    hit = next((t for t in forbidden if t in words), None)
    if hit:
        raise HTTPException(
            status_code=400,
            detail=f"{lang}: write/DDL keyword '{hit}' is not allowed (read-only studio).",
        )
    return query.strip()


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
    responses={400: {"description": "invalid / forbidden query"}, 504: {"description": "timeout"}},
)
@inject
def cypher_query(body: Annotated[dict, Body(...)], neo4j: FromDishka[Neo4jClient]) -> dict:
    """Run a read-only Cypher query against Neo4j. Returns { columns, rows }."""
    query = _validate((body or {}).get("query") or "", _CYPHER_FORBIDDEN, "Cypher")

    def _run(tx):
        res = tx.run(query, timeout=_TIMEOUT_MS / 1000)
        cols = list(res.keys())
        out = []
        for i, rec in enumerate(res):
            if i >= _ROW_CAP:
                return cols, out, True
            out.append([_jsonable(rec[c]) for c in cols])
        return cols, out, False

    try:
        with neo4j.session() as session:
            cols, rows, truncated = session.execute_read(_run)  # read tx rejects writes
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # surface engine/driver errors to the editor rather than 500-ing
        msg = str(exc)
        code = 504 if "timeout" in msg.lower() else 400
        raise HTTPException(status_code=code, detail=f"Cypher error: {msg[:300]}") from exc
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated}


@router.post(
    "/sql",
    responses={
        400: {"description": "invalid / forbidden query"},
        503: {"description": "stats DB unset"},
        504: {"description": "timeout"},
    },
)
def sql_query(body: Annotated[dict, Body(...)]) -> dict:
    """Run a read-only SQL query against the stats Postgres. Returns { columns, rows }."""
    query = _validate((body or {}).get("query") or "", _SQL_FORBIDDEN, "SQL")
    dsn = os.environ.get("STATS_DATABASE_URL")
    if not dsn:
        raise HTTPException(
            status_code=503,
            detail="SQL studio not configured (STATS_DATABASE_URL unset)",
        )
    dsn = (dsn.replace("postgresql+asyncpg://", "postgresql://")
           .replace("postgresql+psycopg://", "postgresql://"))
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.read_only = True  # engine-enforced: any write raises
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {_TIMEOUT_MS}")
                cur.execute(query)
                cols = [d.name for d in cur.description] if cur.description else []
                rows = [[_jsonable(v) for v in r] for r in cur.fetchmany(_ROW_CAP)]
                truncated = cur.fetchone() is not None
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=504, detail="SQL query exceeded the timeout") from exc
    except psycopg.Error as exc:
        raise HTTPException(status_code=400, detail=f"SQL error: {str(exc)[:300]}") from exc
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated}
