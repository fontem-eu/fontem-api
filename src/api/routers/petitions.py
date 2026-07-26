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

# Ordering variants. ``supporters`` (the default) keeps the original clause
# verbatim; ``recent`` surfaces the most recently registered petition first,
# tie-breaking on supporters so the order is deterministic.
_ORDER_SUPPORTERS = (
    "ORDER BY coalesce(p.total_supporters, 0) DESC, "
    "         p.registration_date DESC"
)
_ORDER_RECENT = (
    "ORDER BY coalesce(p.registration_date, '') DESC, "
    "         coalesce(p.total_supporters, 0) DESC"
)

# Bounds on the comma-separated ``statuses`` filter: at most this many tokens,
# each no longer than a single register status code.
_MAX_STATUSES = 10
_MAX_STATUS_LEN = 40


def _parse_statuses(raw: str | None) -> list[str] | None:
    """Split a comma-separated ``statuses`` value into exact status tokens.

    Trims, upper-cases and drops empties; discards over-long tokens and caps
    the list length. Returns ``None`` when nothing usable remains so the
    caller can fall back to the single-``status`` filter.
    """
    if not raw:
        return None
    tokens = [
        tok for tok in (part.strip().upper() for part in raw.split(","))
        if tok and len(tok) <= _MAX_STATUS_LEN
    ]
    tokens = tokens[:_MAX_STATUSES]
    return tokens or None


@router.get("")
@inject
def list_petitions(  # pylint: disable=too-many-arguments
    status: Annotated[str | None, Query(max_length=_MAX_STATUS_LEN)] = None,
    statuses: Annotated[str | None, Query(max_length=500)] = None,
    sort: Annotated[str, Query(pattern="^(supporters|recent)$")] = "supporters",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    *,
    neo4j: FromDishka[Neo4jClient],
) -> dict[str, Any]:
    """Petitions with per-status counts for the filter chips.

    ``statuses`` (comma-separated, exact register vocabulary) filters on a set
    and takes precedence over the single ``status``; ``sort`` picks the order
    (``supporters`` by default, or ``recent`` for most-recently-registered).
    """
    status_list = _parse_statuses(statuses)
    if status_list is not None:
        where = "WHERE p.status IN $statuses"
        params: dict[str, Any] = {"statuses": status_list}
    else:
        where = "WHERE $status IS NULL OR p.status = $status"
        params = {"status": status}
    order = _ORDER_RECENT if sort == "recent" else _ORDER_SUPPORTERS
    with neo4j.session() as session:
        counts = {
            r["status"]: r["n"] for r in session.run(
                "MATCH (p:Petition) RETURN p.status AS status, "
                "count(*) AS n"
            ).data() if r["status"]
        }
        rows = session.run(
            "MATCH (p:Petition) "
            f"{where} "
            f"RETURN {_LIST_FIELDS} "
            f"{order} "
            "SKIP $offset LIMIT $limit",
            offset=offset, limit=limit, **params,
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
