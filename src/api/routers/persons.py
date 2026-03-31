"""
Persons API Router
===================
Endpoints for company directors and officers.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..dependencies import get_person_source

router = APIRouter(prefix="/persons", tags=["persons"])


@router.get("/search")
def search_persons(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    source=Depends(get_person_source),
):
    """Search persons by name."""
    return {"results": source.search_persons(q, limit=limit)}


@router.get("/{person_id}")
def person_detail(
    person_id: str,
    source=Depends(get_person_source),
):
    """Return all roles held by a person."""
    roles = source.get_person_roles(person_id)
    if not roles:
        return {"person_id": person_id, "roles": []}
    return {"person_id": person_id, "roles": roles}
