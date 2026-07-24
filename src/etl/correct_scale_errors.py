"""One-shot history correction for the milli-euro (x1000) gateway leak.

Scans the event log for the latest UpsertContract per contract IRI,
applies `normalize_scale` to the stored monetary fields, and emits a
corrected UpsertContract for every hit — no TED re-download needed:
the correction is deterministic (/1000) and every input is already in
the stored payload. All sinks (neo4j, virtuoso, embeddings) pick the
corrections up through the normal event flow.

Usage:
    python -m src.etl.correct_scale_errors [--dry-run] [--limit N]

Dry-run prints what would be emitted; the real run emits events with
producer="correct_scale_errors" so the batch is auditable later.
"""
from __future__ import annotations

import argparse
import logging
import os
import uuid

import psycopg

from .contract_confidence import score_contract_value
from .scale_normalization import normalize_scale

logger = logging.getLogger(__name__)

# Latest UpsertContract per IRI, restricted to candidates worth
# checking: any monetary field >= 100M EUR. The x1000 leak floor in
# normalize_scale is higher; over-selecting here is fine (normalize
# passes sane rows through untouched).
_CANDIDATE_SQL = """
SELECT DISTINCT ON (iri) iri, payload
FROM events.entity_events
WHERE domain = 'contract'
  AND event_type = 'UpsertContract'
  AND (
    COALESCE(NULLIF(payload->>'value_eur',''),'0')::float >= %(floor)s
    OR COALESCE(NULLIF(payload->>'estimated_value_eur',''),'0')::float >= %(floor)s
    OR COALESCE(NULLIF(payload->>'value_payable_eur',''),'0')::float >= %(floor)s
  )
ORDER BY iri, seq DESC
"""


def _corrected_payload(payload: dict) -> dict | None:
    """Return a corrected copy of the payload, or None if not affected."""
    def _f(key):
        v = payload.get(key)
        return float(v) if v is not None else None

    scale = normalize_scale(
        estimate_eur=_f("estimated_value_eur"),
        total_eur=_f("value_eur"),
        payable_eur=_f("value_payable_eur"),
        total_original=_f("value_original"),
        country=payload.get("country"),
    )
    if not scale.corrected:
        return None

    out = dict(payload)
    if scale.estimate_eur is not None or "estimated_value_eur" in out:
        out["estimated_value_eur"] = scale.estimate_eur
    out["value_eur"] = scale.total_eur if scale.total_eur is not None \
        else scale.payable_eur
    if "value_payable_eur" in out:
        out["value_payable_eur"] = scale.payable_eur
    if "value_original" in out:
        out["value_original"] = scale.total_original \
            if scale.total_original is not None else out["value_eur"]
    out["value_scale_corrected"] = scale.tier

    # Re-score on corrected magnitudes so the confidence fields stop
    # carrying the pre-correction disagreement/implausibility verdicts.
    score = score_contract_value(
        estimate_eur=out.get("estimated_value_eur"),
        total_eur=out.get("value_eur"),
        payable_eur=out.get("value_payable_eur"),
    )
    out["value_quality_flag"] = score.flag.value
    out["value_low_confidence"] = score.is_low_confidence
    out["value_confidence"] = score.confidence
    out["value_confidence_consistency"] = score.consistency
    out["value_confidence_plausibility"] = score.plausibility
    out.pop("value_quarantined", None)
    out.pop("value_quarantine_reason", None)
    return out


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--floor", type=float, default=1e8,
        help="Candidate pre-filter: only events with a monetary field "
             ">= this (EUR) are scanned. Lower for a completeness pass "
             "(the correction rules themselves are unchanged).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )

    dsn = os.environ["EVENTS_DATABASE_URL"]
    corrected = scanned = 0
    log = None
    if not args.dry_run:
        # Lazy import: fontem_events is a cluster-vendored package and
        # not present in local unit-test envs that only exercise
        # _corrected_payload.
        from fontem_events import EventLog  # pylint: disable=import-outside-toplevel
        log = EventLog.from_env()

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_CANDIDATE_SQL, {"floor": args.floor})
        rows = cur.fetchall()
    logger.info("candidates: %d", len(rows))

    for iri, payload in rows:
        scanned += 1
        if args.limit and corrected >= args.limit:
            break
        fixed = _corrected_payload(payload)
        if fixed is None:
            continue
        corrected += 1
        logger.info(
            "%s: %s -> %s (%s)",
            payload.get("ted_notice_id"), payload.get("value_eur"),
            fixed.get("value_eur"), fixed.get("value_scale_corrected"),
        )
        if not args.dry_run:
            with log.batch(
                uuid.uuid4(), producer="correct_scale_errors",
            ) as emit:
                emit.upsert(
                    "UpsertContract", iri=iri, domain="contract",
                    payload=fixed,
                )

    logger.info("scanned=%d corrected=%d dry_run=%s",
                scanned, corrected, args.dry_run)


if __name__ == "__main__":
    main()
