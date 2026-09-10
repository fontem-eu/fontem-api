"""
Consolidator Review Proxy
=========================
The four consolidator endpoints the admin review screen needs, behind
this service's own data-admin gate.

Why a proxy rather than authentication in the consolidator itself:

Most of the consolidator's API is machine-to-machine — the Neo4j APOC
trigger posts to /webhooks/neo4j-trigger, our ETL posts
/consolidate/batch and /resolve, the trigger consumer posts
/events/dispatch — and none of those callers can present a user token.
So a token gate there would have to be per-route anyway, and the
machine routes would still need to leave the public edge.

Meanwhile the browser-facing half is exactly four routes, and every
environment already runs a fontem-api holding its OWN JWT secret.
Proxying through here verifies the token against the secret of the
environment that minted it, so no signing key has to be copied into the
shared data tier. nginx stops publishing the consolidator altogether;
it becomes an internal service, which is what it is.

The routes below are declared one by one on purpose. A catch-all
`{path:path}` forwarder would be shorter and would quietly re-expose
/consolidate, /resolve and the webhooks through this door — the exact
surface this change exists to close. This list is the allowlist.
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.api.admin_auth import require_data_admin

# Namespace-relative by default, which resolves to the local
# consolidator in fontem-prod. fontem-staging and fontem-testing run no
# consolidator of their own and must set this explicitly to the shared
# one — see the chart's `consolidatorUrl`.
CONSOLIDATOR_URL = os.environ.get(
    "CONSOLIDATOR_URL", "http://fontem-consolidator:8000",
).rstrip("/")

# The consolidator shares a pod with the rule engine and can be busy
# during a sweep. This mirrors the ETL hook's ceiling rather than the
# API's usual 90s, because a reviewer is waiting on the other end.
TIMEOUT = float(os.environ.get("CONSOLIDATOR_REVIEW_TIMEOUT", "30"))

router = APIRouter(
    prefix="/consolidator",
    tags=["consolidator-review"],
    dependencies=[Depends(require_data_admin)],
    responses={
        401: {"description": "missing or invalid token"},
        403: {"description": "not a platform data admin"},
        502: {"description": "the consolidator is unreachable"},
    },
)


def _reviewer(claims: dict) -> str:
    """Who the audit log should record.

    The screen used to send a reviewer name it kept in localStorage, so
    the audit trail recorded whatever the client typed. Now that the
    caller is authenticated, identity comes from the token instead.
    """
    return claims.get("email") or claims.get("sub") or "unknown"


async def _json_body(request: Request) -> dict:
    """The request body as a dict, tolerating an empty one.

    Validation stays upstream: the consolidator owns these schemas, and
    duplicating them here would mean two definitions drifting apart.
    """
    try:
        parsed = await request.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {"body": parsed}


async def _forward(
    method: str,
    path: str,
    request: Request,
    body: dict | None = None,
) -> JSONResponse:
    """Pass one call upstream and hand the answer back unchanged.

    Upstream status codes are preserved — the screen relies on 404 and
    409 to tell "already settled" apart from "not there", and
    flattening those into 500 would make it lie to the reviewer.
    """
    url = f"{CONSOLIDATOR_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            upstream = await client.request(
                method, url,
                params=dict(request.query_params) or None,
                json=body,
            )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"consolidator unreachable: {exc.__class__.__name__}"},
        )

    try:
        payload = upstream.json()
    except ValueError:
        payload = {"detail": upstream.text[:500]}
    return JSONResponse(status_code=upstream.status_code, content=payload)


@router.get("/candidates")
async def list_candidates(request: Request) -> JSONResponse:
    """SAME_AS proposals awaiting a decision."""
    return await _forward("GET", "/candidates", request)


@router.get("/relationships")
async def list_relationships(request: Request) -> JSONResponse:
    """REPRESENTS / SANCTIONED edges awaiting review."""
    return await _forward("GET", "/relationships", request)


@router.post("/candidates/{from_id}/{to_id}/decide")
async def decide_candidate(
    from_id: str, to_id: str, request: Request,
    claims: dict = Depends(require_data_admin),
) -> JSONResponse:
    """Approve or decline one SAME_AS proposal."""
    body = await _json_body(request)
    body["reviewer"] = _reviewer(claims)
    return await _forward(
        "POST", f"/candidates/{from_id}/{to_id}/decide", request, body,
    )


@router.post("/relationships/{edge_id}/decide")
async def decide_relationship(
    edge_id: str, request: Request,
    claims: dict = Depends(require_data_admin),
) -> JSONResponse:
    """Accept or reject one relationship-review candidate."""
    body = await _json_body(request)
    body["reviewer"] = _reviewer(claims)
    return await _forward(
        "POST", f"/relationships/{edge_id}/decide", request, body,
    )
