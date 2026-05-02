"""GET /datasets — catalog list."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.atlas_api.schemas import DatasetSummary

router = APIRouter(tags=["atlas"])


def _stats_source(request: Request):
    src = request.app.state.fontem_stats_source
    if not src.configured:
        raise HTTPException(
            status_code=503,
            detail="stats store unavailable (STATS_DATABASE_URL unset)",
        )
    return src


@router.get("/datasets", response_model=list[DatasetSummary])
def list_datasets(request: Request) -> list[DatasetSummary]:
    """Every enabled dataset + its last successful sync."""
    return [DatasetSummary(**row) for row in _stats_source(request).list_datasets()]
