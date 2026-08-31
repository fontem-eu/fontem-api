"""
Shared helpers for API router modules.
"""
from __future__ import annotations

import math

from fastapi import HTTPException, Request


def nan_to_none(value: float) -> float | None:
    """Convert NaN / Inf to None for JSON serialisation."""
    if value is None:
        return None
    try:
        return None if (math.isnan(value) or math.isinf(value)) else value
    except (TypeError, ValueError):
        return None


# The events store is optional: a cluster can run the API without
# EVENTS_DATABASE_URL and every data-quality endpoint that reads it must
# answer 503 rather than 500. The message was written out at each call
# site, which is how it came to be triplicated inside one module.
def events_source_or_503(request: Request):
    """Return the events-DB source, or 503 if it was never configured."""
    src = request.app.state.etl_runs_source
    if not src.configured:
        raise HTTPException(
            status_code=503,
            detail="events store unavailable (EVENTS_DATABASE_URL unset)",
        )
    return src
