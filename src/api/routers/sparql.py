"""
SPARQL Proxy Router
====================
Endpoints:
  - GET  /sparql      — discoverable docs payload (endpoint URL, formats,
                        limits, sample queries). Same shape the docs UI
                        consumes; convenient for clients introspecting
                        without parsing HTML.
  - POST /sparql      — execute a SPARQL SELECT/ASK against the
                        configured Virtuoso. The frontend's
                        interactive editor posts here.

This is a thin proxy with two goals:
  1. Give end users a way to query the knowledge graph without
     having to bring their own SPARQL client + know the cluster-
     internal Virtuoso URL.
  2. Cap query depth so a runaway scan can't tip Virtuoso over.

Heavy authoring (UPDATE / INSERT / DROP / WHERE-rewrite) intentionally
stays inside the ETL writer paths — this endpoint is read-only.
"""
from __future__ import annotations

import logging
from typing import Annotated

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Body, HTTPException

from src.data.sparql.virtuoso_client import SparqlTimeout, VirtuosoClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sparql", tags=["sparql"])


# Max query length we'll forward to Virtuoso. 4 KB is plenty for the
# kind of explore-the-data queries this endpoint is meant for — a
# pasted JSON-LD dump or a malicious payload won't fit. Real ETL
# queries don't use this endpoint.
_MAX_QUERY_BYTES = 4096

# A small allow-list of read-only verbs. SPARQL update keywords are
# rejected — this endpoint is strictly for SELECT / ASK / CONSTRUCT /
# DESCRIBE.
_FORBIDDEN_TOKENS = (
    "INSERT", "DELETE", "DROP", "CLEAR", "CREATE",
    "LOAD", "COPY", "MOVE", "ADD",
)


def _looks_like_update(query: str) -> bool:
    upper = query.upper()
    return any(tok in upper.split() for tok in _FORBIDDEN_TOKENS)


@router.get("")
def sparql_docs() -> dict:
    """Discoverable description of the SPARQL endpoint.

    Mirrors the doc copy on the /sparql page so a CLI client can
    introspect the endpoint without parsing HTML. Stable contract:
    additive changes only.
    """
    return {
        "endpoint": "/api/sparql",
        "methods": ["GET (this doc), POST (execute query)"],
        "request": {
            "POST": {
                "content_type": "application/json",
                "body": {"query": "SPARQL SELECT/ASK/CONSTRUCT/DESCRIBE"},
            },
        },
        "response": {
            "content_type": "application/sparql-results+json",
        },
        "limits": {
            "max_query_bytes": _MAX_QUERY_BYTES,
            "read_only": True,
        },
        "examples": [
            {
                "title": "Sample-five sanctioned entities",
                "query": (
                    "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
                    "PREFIX schema: <https://schema.org/>\n"
                    "SELECT ?entity ?name WHERE {\n"
                    "  GRAPH <http://data.fontem.eu/graph/sanctions> {\n"
                    "    ?entity rdf:type schema:Organization ;\n"
                    "            schema:name ?name .\n"
                    "  }\n"
                    "} LIMIT 5"
                ),
            },
        ],
    }


@router.post(
    "",
    responses={
        400: {"description": "invalid payload or forbidden SPARQL keyword"},
        503: {"description": "VIRTUOSO_SPARQL_URL not configured"},
        504: {"description": "SPARQL query exceeded the configured timeout"},
    },
)
@inject
def sparql_query(
    body: Annotated[dict, Body(...)],
    virtuoso: FromDishka[VirtuosoClient | None],
) -> dict:
    """Run a SPARQL SELECT/ASK against Virtuoso. Returns the raw
    SPARQL JSON results format so any standard SPARQL client (or the
    in-app editor) can render the response without translation.
    """
    query = (body or {}).get("query") or ""
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(
            status_code=400, detail="Body must include a non-empty `query` string",
        )
    if len(query.encode("utf-8")) > _MAX_QUERY_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Query exceeds {_MAX_QUERY_BYTES}-byte limit "
                "for the read-only endpoint"
            ),
        )
    if _looks_like_update(query):
        raise HTTPException(
            status_code=400,
            detail=(
                "SPARQL UPDATE / INSERT / DELETE / DROP / CLEAR / CREATE / "
                "LOAD / COPY / MOVE / ADD are not permitted on the public "
                "endpoint."
            ),
        )

    if virtuoso is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Virtuoso is not configured in this environment "
                "(VIRTUOSO_SPARQL_URL unset)"
            ),
        )

    try:
        bindings = virtuoso.query(query)
    except SparqlTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc

    head_vars = list(bindings[0].keys()) if bindings else []
    out_rows = [{var: _envelope_for(val) for var, val in row.items()}
                for row in bindings]
    return {
        "head": {"vars": head_vars},
        "results": {"bindings": out_rows},
    }


def _envelope_for(value) -> dict:
    """Translate one python value into the SPARQL 1.1 JSON binding
    envelope. VirtuosoClient pre-unwraps datatyped numerics into native
    int/float; this rebuilds the envelope so the in-app editor + any
    standard SPARQL client can render the response uniformly.
    """
    if isinstance(value, bool):
        return {
            "type": "literal",
            "value": "true" if value else "false",
            "datatype": "http://www.w3.org/2001/XMLSchema#boolean",
        }
    if isinstance(value, int):
        return {
            "type": "literal", "value": str(value),
            "datatype": "http://www.w3.org/2001/XMLSchema#integer",
        }
    if isinstance(value, float):
        return {
            "type": "literal", "value": str(value),
            "datatype": "http://www.w3.org/2001/XMLSchema#decimal",
        }
    if isinstance(value, str) and "://" in value:
        return {"type": "uri", "value": value}
    return {"type": "literal", "value": str(value)}
