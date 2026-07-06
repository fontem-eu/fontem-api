"""The contract-value review queue (events.value_review).

When the confidence scorer quarantines a value in the REVIEW tier
(implausible magnitude, negative concession, uncorroborated single
signal) the loader withholds the monetary fields from the emitted
event and parks the claim here for a human decision. The decision
flows back as a corrective UpsertContract event (see the value-review
API router), so it survives re-ingests and replays — the graph is
never hand-edited.

The table lives in the events schema next to the log it snapshots.
DDL is idempotent and applied on first use (the bootstrap ConfigMap
carries the same statement for fresh volumes).
"""
from __future__ import annotations

import logging
import os

import psycopg

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS events.value_review (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ted_notice_id    TEXT NOT NULL,
    reason           TEXT NOT NULL,
    claimed_value_eur      DOUBLE PRECISION,
    claimed_value_original DOUBLE PRECISION,
    claimed_currency       TEXT,
    claimed_estimated_eur  DOUBLE PRECISION,
    claimed_payable_eur    DOUBLE PRECISION,
    detail           TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    corrected_value_eur DOUBLE PRECISION,
    decided_note     TEXT,
    decided_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ted_notice_id)
);
CREATE INDEX IF NOT EXISTS value_review_status_created
    ON events.value_review (status, created_at DESC);
"""


def _dsn() -> str | None:
    dsn = os.environ.get("EVENTS_DATABASE_URL")
    if not dsn or "$(" in dsn:
        return None
    return (dsn.replace("postgresql+asyncpg://", "postgresql://")
               .replace("postgresql+psycopg://", "postgresql://"))


def connect():
    """Connection to the events store, DDL applied. Returns None when
    the env isn't wired (unit tests, environments without the store) —
    callers treat that as 'queue disabled', never as an error."""
    dsn = _dsn()
    if not dsn:
        return None
    conn = psycopg.connect(dsn)
    with conn, conn.cursor() as cur:  # pylint: disable=no-member
        cur.execute(DDL)
    return conn


def enqueue(conn, *, ted_notice_id: str, reason: str,  # pylint: disable=too-many-arguments
            claimed_value_eur=None, claimed_value_original=None,
            claimed_currency=None, claimed_estimated_eur=None,
            claimed_payable_eur=None, detail: str | None = None) -> bool:
    """Insert a pending review row; idempotent per notice (re-ingests
    must not duplicate). Returns True when a new row landed."""
    if conn is None:
        return False
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events.value_review (
                ted_notice_id, reason, claimed_value_eur,
                claimed_value_original, claimed_currency,
                claimed_estimated_eur, claimed_payable_eur, detail
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ted_notice_id) DO NOTHING
            """,
            (ted_notice_id, reason, claimed_value_eur,
             claimed_value_original, claimed_currency,
             claimed_estimated_eur, claimed_payable_eur, detail),
        )
        return cur.rowcount > 0


# Lazy per-process connection so loaders don't thread a handle through
# every call layer. Best-effort by design: a queue hiccup logs and
# moves on — the claimed value is always recoverable from the event
# log, so losing a queue row is an inconvenience, not data loss.
_CONN = None
_CONN_FAILED = False


def enqueue_default(**kwargs) -> bool:
    """enqueue() on a cached default connection; never raises."""
    global _CONN, _CONN_FAILED  # pylint: disable=global-statement
    if _CONN_FAILED:
        return False
    try:
        if _CONN is None or _CONN.closed:  # pylint: disable=no-member
            _CONN = connect()
            if _CONN is None:
                _CONN_FAILED = True
                logger.info("value-review queue disabled "
                            "(EVENTS_DATABASE_URL not set)")
                return False
        return enqueue(_CONN, **kwargs)
    except psycopg.Error as exc:
        logger.warning("value-review enqueue failed for %s: %s "
                       "(claim still in the event log)",
                       kwargs.get("ted_notice_id"), exc)
        try:
            if _CONN is not None:
                _CONN.close()
        except psycopg.Error:
            pass
        _CONN = None
        return False
