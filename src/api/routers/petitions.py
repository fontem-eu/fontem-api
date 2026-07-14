"""Public petitions API (petitions plan P0-P2 surface).

Backs the /petitions pages in the web app: a filterable list and a
detail view that includes the petition's linked legislation (the
:Petition->:LegalAct edges the legislative materializer maintains).

Petition ids contain parentheses (``ECI(2024)000007``), so the detail
endpoint takes query params rather than path segments.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query

from src.data.graph.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/petitions", tags=["petitions"])

_LIST_FIELDS = (
    "p.system AS system, p.petition_id AS petition_id, p.title AS title, "
    "p.status AS status, p.total_supporters AS total_supporters, "
    "p.registration_date AS registration_date, "
    "p.answered_date AS answered_date, p.latest_update AS latest_update"
)


@router.get("")
@inject
def list_petitions(
    status: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    *,
    neo4j: FromDishka[Neo4jClient],
) -> dict[str, Any]:
    """Petitions ordered by supporters, with per-status counts for the
    filter chips. ``status`` filters exactly (register vocabulary)."""
    with neo4j.session() as session:
        counts = {
            r["status"]: r["n"] for r in session.run(
                "MATCH (p:Petition) RETURN p.status AS status, "
                "count(*) AS n"
            ).data() if r["status"]
        }
        rows = session.run(
            "MATCH (p:Petition) "
            "WHERE $status IS NULL OR p.status = $status "
            f"RETURN {_LIST_FIELDS} "
            "ORDER BY coalesce(p.total_supporters, 0) DESC, "
            "         p.registration_date DESC "
            "SKIP $offset LIMIT $limit",
            status=status, offset=offset, limit=limit,
        ).data()
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "results": rows,
    }


@router.get(
    "/detail",
    responses={404: {"description": "No petition with this system/id."}},
)
@inject
def petition_detail(
    petition_id: Annotated[str, Query(min_length=3, max_length=60)],
    system: Annotated[str, Query(max_length=40)] = "eu-eci",
    *,
    neo4j: FromDishka[Neo4jClient],
) -> dict[str, Any]:
    """One petition with its linked legislation.

    Legislation buckets: REGISTERED_BY (the registration decision),
    ANSWERED_BY (the Commission's answer document) and LED_TO
    (explicitly named follow-up acts). Unresolved answer refs are
    surfaced verbatim so the page can say "answer documented, not
    yet linkable" instead of hiding it.
    """
    with neo4j.session() as session:
        rows = session.run(
            "MATCH (p:Petition {system: $system, petition_id: $pid}) "
            "OPTIONAL MATCH (p)-[r:REGISTERED_BY|ANSWERED_BY|LED_TO]"
            "->(a:LegalAct) "
            "RETURN p AS petition, collect({rel: type(r), celex: a.celex, "
            "  title_en: a.title_en, title_fr: a.title_fr, "
            "  date: a.date_document, doc_type: a.doc_type}) AS acts",
            system=system, pid=petition_id,
        ).data()
    if not rows:
        raise HTTPException(status_code=404, detail="petition not found")
    petition = dict(rows[0]["petition"])
    acts = [a for a in rows[0]["acts"] if a.get("celex")]
    for a in acts:
        a["eurlex_url"] = (
            "https://eur-lex.europa.eu/legal-content/EN/TXT/"
            f"?uri=CELEX:{a['celex']}"
        )
    linked = {a["celex"] for a in acts if a.get("rel") == "ANSWERED_BY"}
    unresolved = [
        ref for ref in (petition.get("answer_refs") or []) if ref not in linked
    ]
    return {
        "petition": petition,
        "legislation": acts,
        "unresolved_answer_refs": unresolved,
    }
