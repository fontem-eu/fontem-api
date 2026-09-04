"""
Lobbyists API Router
====================
The EU Transparency Register entry for one organisation.

A lobbying declaration is a thing in its own right, not a footnote on a
company page: only 3,565 of 18,195 registrants resolve to a company we
hold, so routing these through /company/ left roughly four in five with
nowhere to go.

`disclosure_id` is the key because it is the only identifier these nodes
carry. `gmr_id`, `tr_id` and `transparency_register_id` are each present
on ZERO Lobbyist nodes, despite graph.py and mentions.py having
referenced the latter two.
"""
from __future__ import annotations

from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException

from src.data.graph.neo4j_client import Neo4jClient


router = APIRouter(prefix="/lobbyists", tags=["lobbyists"])


def _money(node: dict[str, Any]) -> dict[str, Any] | None:
    """The declared annual lobbying spend, as the register states it.

    The register collects a BAND, not a figure, and a band with only one
    end is still information ("at least 10M") — so this returns whatever
    is present rather than requiring both.
    """
    low, high = node.get("detail_cost_min"), node.get("detail_cost_max")
    if low is None and high is None:
        return None
    return {"min_eur": low, "max_eur": high, "currency": "EUR"}


def _profile(node: dict[str, Any], filed_for: list[dict]) -> dict[str, Any]:
    return {
        "disclosure_id": node.get("disclosure_id"),
        "name": node.get("detail_name"),
        "acronym": node.get("detail_acronym"),
        "category": node.get("detail_category"),
        "entity_form": node.get("detail_entity_form"),
        "country": node.get("detail_country"),
        "country_iso": node.get("detail_country_iso"),
        "city": node.get("detail_city"),
        "website": node.get("detail_website"),
        "goals": node.get("detail_goals"),
        "interests": node.get("detail_interests"),
        "declared_spend": _money(node),
        "members_fte": node.get("detail_members_fte"),
        "registered_on": node.get("detail_registration_date"),
        "last_updated": node.get("detail_last_updated"),
        "active": node.get("detail_active"),
        # The register's own page for this registrant. Kept distinct
        # from `website`, which is the organisation's own site.
        "register_url": node.get("url"),
        # Entities that filed this declaration. Empty for most, which is
        # the honest answer rather than a reason to hide the page.
        "filed_for": filed_for,
    }


@router.get(
    "/{disclosure_id}",
    responses={
        404: {"description": "no register entry with that disclosure_id"},
    },
)
@inject
def lobbyist_detail(
    disclosure_id: str,
    *,
    neo4j: FromDishka[Neo4jClient],
) -> dict[str, Any]:
    """One registrant, plus whoever filed the declaration."""
    with neo4j.session() as session:
        # Single indexed seek on disclosure_id (see graph_schema). One
        # property, no OR across several — a disjunction over different
        # properties cannot use the index and label-scans instead.
        record = session.run(
            "MATCH (l:Lobbyist {disclosure_id: $did}) "
            "OPTIONAL MATCH (l)-[:FILED_BY]->(e) "
            "RETURN l AS lobbyist, "
            "  collect({label: labels(e)[0], name: e.name, "
            "           gmr_id: e.gmr_id}) AS filed_for "
            "LIMIT 1",
            did=disclosure_id,
        ).single()
        if not record or record["lobbyist"] is None:
            raise HTTPException(status_code=404, detail="lobbyist not found")
        node = dict(record["lobbyist"])
        # OPTIONAL MATCH yields one all-null row when nothing matched;
        # a filer without a name is not one we can link to anyway.
        filed_for = [
            {
                "label": e["label"],
                "name": e["name"],
                # The profile route only exists for entities that have a
                # gmr_id; without one there is nowhere to send a reader.
                "profile": f"/company/{e['gmr_id']}"
                           if e["label"] == "Company" and e.get("gmr_id") else None,
            }
            for e in (record["filed_for"] or [])
            if e and e.get("name")
        ]
    return _profile(node, filed_for)
