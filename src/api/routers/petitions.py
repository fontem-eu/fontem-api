"""Public petitions API (petitions plan P0-P2 surface).

Backs the /petitions pages in the web app: a filterable list and a
detail view that includes the petition's linked legislation (the
:Petition->:LegalAct edges the legislative materializer maintains).

Petition ids contain parentheses (``ECI(2024)000007``), so the detail
endpoint takes query params rather than path segments.
"""
from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query

from src.api.lang import EU_LANGS, safe_lang
from src.data.graph.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/petitions", tags=["petitions"])

#: The list's columns, besides the texts _localised picks.
_LIST_COLUMNS = ("system", "petition_id", "status", "total_supporters",
                 "registration_date", "answered_date", "latest_update")

#: Per-language properties: read through _localised, not returned raw.
_PER_LANGUAGE = re.compile(r"^(?:title|objectives|annex_text|objectives_summary)_[a-z]{2}$")


def _localised(node: dict[str, Any], lang: str | None) -> dict[str, Any]:
    """The petition's texts in the reader's language, beside the original.

    Title, objectives and annex come from the register's official
    versions (title_<lang>, ...); the summary is machine-written, from the
    English objectives, and shown only while it summarises them as they
    read now. Without a version in ``lang``, the English one stands."""
    out = {k: v for k, v in node.items() if not _PER_LANGUAGE.match(k)}
    shown = lang if lang and node.get(f"title_{lang}") else None
    original = node.get("title_lang")
    for field in ("title", "objectives", "annex_text"):
        if shown and node.get(f"{field}_{shown}"):
            out[field] = node[f"{field}_{shown}"]
        out[f"{field}_original"] = (node.get(f"{field}_{original}") if original else None) \
            or node.get(field)
    out["summary"] = None
    if node.get("objectives_summarized_from") == node.get("objectives"):
        in_lang = node.get(f"objectives_summary_{lang}") if lang else None
        out["summary"] = in_lang or node.get("objectives_summary_en")
    out["language_shown"] = shown or "en"
    out["languages"] = sorted(code for code in EU_LANGS if node.get(f"title_{code}"))
    return out

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
    *,
    status: Annotated[str | None, Query(max_length=_MAX_STATUS_LEN)] = None,
    statuses: Annotated[str | None, Query(max_length=500)] = None,
    sort: Annotated[str, Query(pattern="^(supporters|recent)$")] = "supporters",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    lang: Annotated[str | None, Query(max_length=10)] = None,
    neo4j: FromDishka[Neo4jClient],
) -> dict[str, Any]:
    """Petitions with per-status counts for the filter chips; each with its
    title and summary in the reader's ``lang`` where there is one.

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
            "RETURN properties(p) AS p "
            f"{order} "
            "SKIP $offset LIMIT $limit",
            offset=offset, limit=limit, **params,
        ).data()
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "results": [_list_row(row["p"], safe_lang(lang)) for row in rows],
    }


def _list_row(node: dict[str, Any], lang: str | None) -> dict[str, Any]:
    """One petition as the list shows it, title and summary localised."""
    local = _localised(node, lang)
    return {**{k: local.get(k) for k in _LIST_COLUMNS},
            "title": local.get("title"), "title_original": local.get("title_original"),
            "summary": local.get("summary"), "language_shown": local.get("language_shown")}


@router.get(
    "/detail",
    responses={404: {"description": "No petition with this system/id."}},
)
@inject
def petition_detail(
    petition_id: Annotated[str, Query(min_length=3, max_length=60)],
    system: Annotated[str, Query(max_length=40)] = "eu-eci",
    lang: Annotated[str | None, Query(max_length=10)] = None,
    *,
    neo4j: FromDishka[Neo4jClient],
) -> dict[str, Any]:
    """One petition with its linked legislation, its texts in the reader's
    ``lang`` where the register publishes that version.

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
    petition = _localised(dict(rows[0]["petition"]), safe_lang(lang))
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
