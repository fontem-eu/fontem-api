"""
Entity Resolution API Router
==============================
Endpoints for reviewing and resolving SAME_AS merge candidates.
"""
from __future__ import annotations

from dishka.integrations.fastapi import FromDishka, inject
from src.analysis.contract_data_source import ContractDataSource
from src.data.graph.neo4j_client import Neo4jClient

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel


router = APIRouter(prefix="/entity-resolution", tags=["entity-resolution"])


class MergedProperties(BaseModel):
    """Operator-edited properties for the merged entity."""

    name: str | None = None
    country: str | None = None
    lei: str | None = None
    vat: list[str] | None = None  # list of known VAT numbers


class MergeDecision(BaseModel):
    """Operator decision on a SAME_AS candidate."""

    action: str  # "approve" | "reject"
    canonical_gmr_id: str | None = None
    merged_properties: MergedProperties | None = None


@router.get("/candidates")
@inject
def list_candidates(
    limit: int = Query(50, ge=1, le=200),
    *,
    neo4j: FromDishka[Neo4jClient],
    source: FromDishka[ContractDataSource],
):
    """List unreviewed SAME_AS merge candidates."""
    with neo4j.session() as session:  
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
@inject
def find_similar(
    name: str = Query(..., min_length=1),
    entity_type: str = Query("company"),
    country: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    *,
    neo4j: FromDishka[Neo4jClient],
    source: FromDishka[ContractDataSource],
):
    """Find similar entities (for manual matching / operator review)."""
    with neo4j.session() as session:  
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


def _validate_merged_properties(props: MergedProperties) -> list[str]:
    """Validate operator-edited properties. Returns list of error messages."""
    import pycountry  # pylint: disable=import-outside-toplevel
    errors = []
    if props.name is not None:
        name = props.name.strip()
        if len(name) < 2:
            errors.append("Name must be at least 2 characters")
        if len(name) > 300:
            errors.append("Name must be at most 300 characters")
    if props.country is not None:
        country = props.country.strip().upper()
        if not pycountry.countries.get(alpha_3=country):
            errors.append(f"Country '{country}' is not a valid ISO alpha-3 code")
    if props.lei is not None and props.lei.strip():
        lei = props.lei.strip()
        if len(lei) != 20:
            errors.append(f"LEI must be exactly 20 characters (got {len(lei)})")
        if not lei.isalnum():
            errors.append("LEI must be alphanumeric")
    if props.vat is not None:
        for i, vat in enumerate(props.vat):
            v = vat.strip()
            if not v:
                continue
            if len(v) < 4:
                errors.append(f"VAT #{i + 1} too short ({len(v)} chars)")
            if len(v) > 50:
                errors.append(f"VAT #{i + 1} too long ({len(v)} chars)")
    return errors


@router.post("/resolve/{dup_id}/{canonical_id}")
@inject
def resolve_candidate(
    dup_id: str,
    canonical_id: str,
    decision: MergeDecision,
    *,
    neo4j: FromDishka[Neo4jClient],
):
    """Approve or reject a SAME_AS merge candidate.

    When approving, merged_properties allows the operator to override
    any field on the surviving entity. All edits are validated.
    """
    # Validate before opening a transaction
    if decision.action not in ("approve", "reject"):
        raise HTTPException(
            status_code=400, detail="action must be 'approve' or 'reject'",
        )
    if decision.action == "approve" and decision.merged_properties:
        errors = _validate_merged_properties(decision.merged_properties)
        if errors:
            raise HTTPException(status_code=422, detail={"validation_errors": errors})

    with neo4j.session() as session:
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

        # Approve: audit + merge + property overrides in a single write tx
        def _merge_tx(tx):
            tx.run(
                "MATCH (dup:Company {gmr_id: $dup}) "
                "CREATE (:MergeEvent {"
                "  canonical_id: $can, merged_id: $dup, "
                "  merged_at: datetime(), method: 'operator_review', "
                "  dup_name: dup.name, dup_country: dup.country"
                "})",
                dup=dup_id, can=canonical_id,
            )
            tx.run(
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
            if decision.merged_properties:
                props = decision.merged_properties
                sets = []
                params = {"can": canonical_id}
                if props.name is not None:
                    sets.append("c.name = $name")
                    params["name"] = props.name.strip()
                if props.country is not None:
                    sets.append("c.country = $country")
                    params["country"] = props.country.strip().upper()
                if props.lei is not None:
                    sets.append("c.lei = $lei")
                    params["lei"] = props.lei.strip() or None
                if props.vat is not None:
                    cleaned = [v.strip() for v in props.vat if v.strip()]
                    sets.append("c.vat = $vat")
                    params["vat"] = cleaned if cleaned else None
                if sets:
                    tx.run(
                        f"MATCH (c:Company {{gmr_id: $can}}) SET {', '.join(sets)}",
                        **params,
                    )

        session.execute_write(_merge_tx)
        return {"status": "merged", "surviving_id": canonical_id}
