"""Contract-value review queue API.

Serves the quarantined-value queue (events.value_review) and turns a
reviewer's decision into a corrective UpsertContract event — the graph
is never hand-edited, so decisions survive re-ingests and replays.

Exposure matches the platform's other review surface (the consolidator
candidates queue behind /api/consolidator/): cluster-internal service
proxied by the web tier, rate-limited, no per-user auth yet — flagged
as a shared gap in the roadmap.
"""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException
from fontem_event_schemas import builders
from fontem_events import EventLog

from src.etl import value_review_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/value-review", tags=["value-review"])

_REASON_EXPLANATIONS = {
    "implausible_magnitude": (
        "The published value is orders of magnitude beyond any plausible "
        "public contract (the scorer decays confidence above EUR 1B; this "
        "one fell off the scale). Usually a currency or price-per-unit "
        "misparse in the source notice."),
    "concession_negative": (
        "A negative value on a concession notice — an accounting artifact "
        "of how the buyer filled the form, not a price."),
    "unverified_single_signal": (
        "Only one uncorroborated money field was published and it "
        "disagrees with nothing because there is nothing to check it "
        "against; magnitude alone made it suspect."),
    "zero_value": (
        "The notice publishes a value of exactly 0 — non-disclosure "
        "wearing a number. Withheld automatically; not human-reviewed."),
}


def _rows(cur) -> list[dict]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _conn():
    conn = value_review_queue.connect()
    if conn is None:
        raise HTTPException(status_code=503,
                            detail="events store not configured")
    return conn


@router.get(
    "",
    responses={503: {"description": "events store not configured"}},
)
def list_reviews(status: str = "pending", limit: int = 200):
    """The queue, newest first. status: pending|corrected|confirmed_bogus|all."""
    where = "" if status == "all" else "WHERE status = %(status)s"
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, ted_notice_id, reason, claimed_value_eur,
                   claimed_value_original, claimed_currency,
                   claimed_estimated_eur, claimed_payable_eur, detail,
                   status, corrected_value_eur, decided_note, decided_at,
                   created_at
            FROM events.value_review {where}
            ORDER BY created_at DESC LIMIT %(limit)s
            """,
            {"status": status, "limit": min(limit, 1000)},
        )
        items = _rows(cur)
        cur.execute(
            "SELECT status, count(*) FROM events.value_review GROUP BY 1")
        counts = dict(cur.fetchall())
    for it in items:
        it["explanation"] = _REASON_EXPLANATIONS.get(it["reason"], "")
    return {"items": items, "counts": counts,
            "explanations": _REASON_EXPLANATIONS}


@router.post(
    "/{review_id}/decide",
    responses={
        400: {"description": "bad action or value_eur"},
        404: {"description": "unknown review id"},
        409: {"description": "already decided"},
        503: {"description": "events store not configured"},
    },
)
def decide(review_id: int, body: Annotated[dict, Body(...)]):
    """Resolve one review. body.action: 'correct' (with value_eur, and
    optionally value_original + currency) or 'confirm_bogus'. Either way
    the outcome is emitted as a corrective UpsertContract event."""
    action = body.get("action")
    if action not in ("correct", "confirm_bogus"):
        raise HTTPException(status_code=400,
                            detail="action must be correct|confirm_bogus")
    note = (body.get("note") or "").strip() or None

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT ted_notice_id, status FROM events.value_review "
                    "WHERE id = %s", (review_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown review id")
        ted_notice_id, status = row
        if status != "pending":
            raise HTTPException(status_code=409,
                                detail=f"already decided: {status}")

        if action == "correct":
            try:
                value_eur = float(body["value_eur"])
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="correct requires a numeric value_eur") from exc
            if not 0 < value_eur < 1e12:
                raise HTTPException(status_code=400,
                                    detail="value_eur outside sane range")
            payload = builders.upsert_contract(
                ted_notice_id=ted_notice_id,
                value_eur=value_eur,
                value_original=body.get("value_original"),
                value_currency=body.get("value_currency"),
                value_confidence=1.0,           # human-verified
                value_quality_flag="ok",
                value_low_confidence=False,
                value_quarantined=False,
            )
        else:
            # Confirmed bogus: the value stays withheld; refresh the
            # marker so replays converge on the reviewed state.
            payload = builders.upsert_contract(
                ted_notice_id=ted_notice_id,
                value_quarantined=True,
                value_quarantine_reason="confirmed_bogus",
            )

        log = EventLog.from_env()
        with log.batch(uuid.uuid4(), producer="value_review") as emit:
            emit.upsert(
                "UpsertContract",
                iri=f"http://data.fontem.eu/id/Contract/{ted_notice_id}",
                domain="contract",
                payload=payload,
            )

        cur.execute(
            """
            UPDATE events.value_review
            SET status = %s, corrected_value_eur = %s,
                decided_note = %s, decided_at = now()
            WHERE id = %s
            """,
            ("corrected" if action == "correct" else "confirmed_bogus",
             body.get("value_eur") if action == "correct" else None,
             note, review_id),
        )
    logger.info("value-review %s: %s %s", review_id, action, ted_notice_id)
    return {"ok": True, "action": action, "ted_notice_id": ted_notice_id}
