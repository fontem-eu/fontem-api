"""Persist assertion results to the events store (events.dq_result).

The events Postgres already holds run/pipeline metadata (etl_run,
consumer_offsets, dead_letter); assertion outcomes are the same class
of operational fact. History is kept per run so the monitor can say
"failing since", not just "failing now".
"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg

_DDL = """
CREATE TABLE IF NOT EXISTS events.dq_result (
    id           bigserial PRIMARY KEY,
    run_at       timestamptz NOT NULL,
    assertion_id text        NOT NULL,
    family       text        NOT NULL,
    severity     text        NOT NULL,
    status       text        NOT NULL,
    observed     text
);
CREATE INDEX IF NOT EXISTS dq_result_assertion_run
    ON events.dq_result (assertion_id, run_at DESC);
CREATE INDEX IF NOT EXISTS dq_result_run
    ON events.dq_result (run_at DESC);
"""


def persist_results(dsn: str, results) -> int:
    """Write one row per assertion result, all sharing a single run_at.
    Self-bootstrapping (CREATE IF NOT EXISTS — the events-schema
    convention), so every environment grows the table on first run."""
    run_at = datetime.now(timezone.utc)
    rows = [(run_at, r.id, r.family, r.severity, r.status,
             r.observed) for r in results]
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)
            cur.executemany(
                "INSERT INTO events.dq_result "
                "(run_at, assertion_id, family, severity, status, observed) "
                "VALUES (%s, %s, %s, %s, %s, %s)", rows)
        conn.commit()
    return len(rows)
