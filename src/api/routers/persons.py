"""
Persons API Router
===================
Endpoints for company directors and officers.
"""
from __future__ import annotations

from dishka.integrations.fastapi import FromDishka, inject
from src.analysis.person_data_source import PersonDataSource

from fastapi import APIRouter, Depends, Query


router = APIRouter(prefix="/persons", tags=["persons"])


@router.get("/search")
@inject
def search_persons(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    *,
    source: FromDishka[PersonDataSource],
):
    """Search persons by name."""
    return {"results": source.search_persons(q, limit=limit)}


@router.get("/{person_id}")
@inject
def person_detail(
    person_id: str,
    source: FromDishka[PersonDataSource],
):
    """Return all roles held by a person."""
    roles = source.get_person_roles(person_id)
    if not roles:
        return {"person_id": person_id, "roles": []}
    return {"person_id": person_id, "roles": roles}
