"""GET /health — liveness + per-source health.

Returns 200 always (the API itself is up), but reports `degraded` in
the body when any source is misconfigured or unreachable. K8s liveness
probes can hit `/health` without expecting all data sources to be up;
readiness probes can branch on the status field if needed.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from src.atlas_api.schemas import AtlasHealth

router = APIRouter()


@router.get("/health", response_model=AtlasHealth, tags=["atlas"])
def health(request: Request) -> AtlasHealth:
    """Aggregate health: 200 always, body reports per-source state."""
    sources = request.app.state.atlas_sources
    statuses = [s.health() for s in sources]
    overall = (
        "ok" if all(s.status == "ok" for s in statuses) else "degraded"
    )
    return AtlasHealth(status=overall, sources=statuses)
