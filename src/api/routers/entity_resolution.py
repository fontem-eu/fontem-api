"""
Entity Resolution API Router
==============================
Endpoints for reviewing and resolving SAME_AS merge candidates.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..dependencies import get_contract_source

router = APIRouter(prefix="/entity-resolution", tags=["entity-resolution"])


class MergeDecision(BaseModel):
    """Operator decision on a SAME_AS candidate."""

    action: str  # "approve" | "reject"
    canonical_gmr_id: str | None = None  # which side wins (for approve)


@router.get("/candidates")
def list_candidates(
    limit: int = Query(50, ge=1, le=200),
    source=Depends(get_contract_source),
):
    """List unreviewed SAME_AS merge candidates."""
    with source._neo4j.session() as session:  # pylint: disable=protected-access
        rows = session.run(
            "MATCH (dup)-[r:SAME_AS {reviewed: false}]->(canonical) "
            "RETURN dup.gmr_id AS dup_id, dup.name AS dup_name, "
            "  dup.country AS dup_country, dup.lei AS dup_lei, "
            "  dup.vat AS dup_vat, "
            "  canonical.gmr_id AS canonical_id, "
            "  canonical.name AS canonical_name, "
            "  canonical.country AS canonical_country, "
            "  canonical.lei AS canonical_lei, "
            "  canonical.vat AS canonical_vat, "
            "  r.confidence AS confidence, r.method AS method, "
            "  r.detected_at AS detected_at "
            "ORDER BY r.confidence DESC "
            "LIMIT $limit",
            limit=limit,
        ).data()
    return {"candidates": rows, "count": len(rows)}


@router.get("/similar")
def find_similar(
    name: str = Query(..., min_length=1),
    entity_type: str = Query("company"),
    country: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    source=Depends(get_contract_source),
):
    """Find similar entities (for manual matching / operator review)."""
    with source._neo4j.session() as session:  # pylint: disable=protected-access
        if entity_type == "authority":
            rows = session.run(
                "MATCH (a:Authority) "
                "WHERE toLower(a.name) CONTAINS toLower($name) "
                + ("AND a.country = $country " if country else "")
                + "RETURN a.authority_id AS id, a.name AS name, "
                "  a.country AS country "
                "LIMIT $limit",
                name=name, country=country, limit=limit,
            ).data()
        else:
            rows = session.run(
                "MATCH (c:Company) "
                "WHERE toLower(c.name) CONTAINS toLower($name) "
                + ("AND c.country = $country " if country else "")
                + "OPTIONAL MATCH (c)-[:LISTED_AS]->(l:Listing) "
                "RETURN c.gmr_id AS id, c.name AS name, "
                "  c.country AS country, c.lei AS lei, "
                "  c.vat AS vat, l.ticker AS ticker "
                "LIMIT $limit",
                name=name, country=country, limit=limit,
            ).data()
    return {"results": rows}


@router.post("/resolve/{dup_id}/{canonical_id}")
def resolve_candidate(
    dup_id: str,
    canonical_id: str,
    decision: MergeDecision,
    source=Depends(get_contract_source),
):
    """Approve or reject a SAME_AS merge candidate."""
    with source._neo4j.session() as session:  # pylint: disable=protected-access
        # Verify the SAME_AS relationship exists
        rel = session.run(
            "MATCH (dup:Company {gmr_id: $dup})"
            "-[r:SAME_AS]->(canonical:Company {gmr_id: $can}) "
            "RETURN r",
            dup=dup_id, can=canonical_id,
        ).single()
        if not rel:
            raise HTTPException(
                status_code=404, detail="SAME_AS relationship not found",
            )

        if decision.action == "reject":
            session.run(
                "MATCH (dup:Company {gmr_id: $dup})"
                "-[r:SAME_AS]->(canonical:Company {gmr_id: $can}) "
                "SET r.reviewed = true, r.verdict = 'rejected'",
                dup=dup_id, can=canonical_id,
            )
            return {"status": "rejected"}

        if decision.action == "approve":
            # Create audit node before merge
            session.run(
                "MATCH (dup:Company {gmr_id: $dup}) "
                "CREATE (:MergeEvent {"
                "  canonical_id: $can, merged_id: $dup, "
                "  merged_at: datetime(), method: 'operator_review', "
                "  dup_name: dup.name, dup_country: dup.country"
                "})",
                dup=dup_id, can=canonical_id,
            )
            # Merge: transfer all relationships from dup to canonical
            session.run(
                "MATCH (dup:Company {gmr_id: $dup}), "
                "  (canonical:Company {gmr_id: $can}) "
                "CALL apoc.refactor.mergeNodes("
                "  [canonical, dup], "
                "  {properties: 'combine', mergeRels: true}"
                ") YIELD node "
                "SET node.gmr_id = $can "
                "RETURN node",
                dup=dup_id, can=canonical_id,
            )
            return {"status": "merged", "surviving_id": canonical_id}

        raise HTTPException(
            status_code=400, detail="action must be 'approve' or 'reject'",
        )
