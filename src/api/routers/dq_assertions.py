"""Assertion monitor: the latest persisted dq-assert run, failing rows
only, enriched from the catalog (title + description) and annotated
with how long each has been failing (events.dq_result history).
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from fastapi import APIRouter, HTTPException

from src.data_quality.assertions.catalog import by_id

router = APIRouter(prefix="/data-quality", tags=["data-quality"])

_LATEST_RUN = "SELECT MAX(run_at) FROM events.dq_result"
_RUN_ROWS = """
SELECT assertion_id, family, severity, status, observed
FROM events.dq_result WHERE run_at = %s ORDER BY
  CASE severity WHEN 'block' THEN 0 ELSE 1 END,
  CASE status WHEN 'error' THEN 0 WHEN 'fail' THEN 1 ELSE 2 END,
  assertion_id
"""
# For each failing assertion: the last time it passed (failing_since is
# the first run after that; NULL last pass = failing for all history).
_LAST_PASS = """
SELECT MAX(run_at) FROM events.dq_result
WHERE assertion_id = %s AND status = 'pass'
"""
_FIRST_FAIL_AFTER = """
SELECT MIN(run_at) FROM events.dq_result
WHERE assertion_id = %s AND status <> 'pass'
  AND (%s::timestamptz IS NULL OR run_at > %s)
"""


@contextmanager
def _connect():
    dsn = os.environ.get("EVENTS_DATABASE_URL", "")
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    if not dsn or "$(" in dsn:
        raise HTTPException(status_code=503,
                            detail="events store unavailable")
    conn = psycopg.connect(dsn, connect_timeout=5)
    try:
        yield conn
    finally:
        conn.close()  # pylint: disable=no-member


def get_assertion_monitor(conn) -> dict:
    catalog = by_id()
    with conn.cursor() as cur:
        cur.execute(_LATEST_RUN)
        run_at = cur.fetchone()[0]
        if run_at is None:
            return {"run_at": None, "summary": None, "failing": []}
        cur.execute(_RUN_ROWS, (run_at,))
        rows = cur.fetchall()
        failing = []
        summary = {"pass": 0, "warn": 0, "fail": 0, "error": 0}
        for aid, family, severity, status, observed in rows:
            summary[status] = summary.get(status, 0) + 1
            if status == "pass":
                continue
            cur.execute(_LAST_PASS, (aid,))
            last_pass = cur.fetchone()[0]
            cur.execute(_FIRST_FAIL_AFTER, (aid, last_pass, last_pass))
            failing_since = cur.fetchone()[0]
            a = catalog.get(aid)
            failing.append({
                "id": aid, "family": family, "severity": severity,
                "status": status, "observed": observed,
                "title": a.title if a else aid,
                "description": a.rationale if a else "",
                "failing_since": failing_since.isoformat() if failing_since else None,
                "last_pass_at": last_pass.isoformat() if last_pass else None,
            })
    return {"run_at": run_at.isoformat(), "summary": summary,
            "failing": failing}


@router.get(
    "/assertions",
    responses={503: {"description": "events store unavailable"}},
)
def assertion_monitor():
    """Latest dq-assert run: failing assertions only, with descriptions
    and failing-since. Refreshed by the dq-assert-monitor CronJob."""
    with _connect() as conn:
        return get_assertion_monitor(conn)
