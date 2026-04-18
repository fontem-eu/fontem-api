"""Postgres adapter for reasoner findings + audit log.

Uses psycopg2 directly to stay dependency-light (the rest of the ETL
package doesn't depend on SQLAlchemy). The DB is gmr-community-api's
Postgres — same DATABASE_URL / POSTGRES_PASSWORD env vars the API
uses.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg2
import psycopg2.extras

from .rule import Finding

logger = logging.getLogger(__name__)


def _dsn() -> str:
    """Compose a libpq DSN from env vars.

    gmr-community-api uses DATABASE_URL in SQLAlchemy async form
    (postgresql+asyncpg://...). psycopg2 wants postgresql://, so we
    strip the driver suffix if present, and substitute $POSTGRES_PASSWORD
    if the URL uses that template.
    """
    raw = os.environ.get(
        "REASONER_DATABASE_URL",
        os.environ.get("DATABASE_URL", ""),
    )
    if not raw:
        raise RuntimeError(
            "Neither REASONER_DATABASE_URL nor DATABASE_URL is set",
        )
    # Strip SQLAlchemy driver suffix (postgresql+asyncpg -> postgresql).
    raw = raw.replace("postgresql+asyncpg://", "postgresql://", 1)
    # Substitute env var template used in k8s manifests.
    pg_pw = os.environ.get("POSTGRES_PASSWORD")
    if pg_pw and "$(POSTGRES_PASSWORD)" in raw:
        raw = raw.replace("$(POSTGRES_PASSWORD)", pg_pw)
    return raw


@contextmanager
def _connection() -> Iterator[Any]:
    """Per-call connection. Reasoner runs are short, no pooling needed."""
    conn = psycopg2.connect(_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class Persistence:
    """Upserts reasoner findings and appends audit rows."""

    def upsert_finding(self, finding: Finding) -> None:
        """Insert a finding or bump its last_seen_at if already present.

        Dedup key is (rule_id, finding_key). Idempotent across sweeps.
        """
        self._upsert_many([finding])

    def upsert_many(self, findings: list[Finding]) -> int:
        """Batch version. Returns the number of rows written."""
        return self._upsert_many(findings)

    def _upsert_many(self, findings: list[Finding]) -> int:
        if not findings:
            return 0
        rows = [
            (
                f.rule_id,
                f.finding_key(),
                f.severity,
                f.confidence,
                json.dumps(sorted(f.target_ids)),
                f.message,
                json.dumps(f.payload),
            )
            for f in findings
        ]
        with _connection() as conn, conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO reasoner_findings
                  (rule_id, finding_key, severity, confidence,
                   target_ids, message, payload)
                VALUES %s
                ON CONFLICT (rule_id, finding_key) DO UPDATE SET
                  severity     = EXCLUDED.severity,
                  confidence   = EXCLUDED.confidence,
                  target_ids   = EXCLUDED.target_ids,
                  message      = EXCLUDED.message,
                  payload      = EXCLUDED.payload,
                  last_seen_at = now(),
                  status       = CASE
                    WHEN reasoner_findings.status IN ('resolved','dismissed')
                    THEN reasoner_findings.status
                    ELSE 'open'
                  END
                """,
                rows,
            )
        return len(rows)

    def record_audit(
        self,
        rule_id: str,
        finding_key: str,
        run_id: str,
        action: str,
        summary: str,
        payload: Optional[dict] = None,
    ) -> None:
        """Append an immutable row to reasoner_audit."""
        with _connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reasoner_audit
                  (rule_id, finding_key, run_id, action, summary, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    rule_id,
                    finding_key,
                    run_id,
                    action,
                    summary,
                    json.dumps(payload or {}),
                ),
            )

    def mark_applied(self, rule_id: str, finding_key: str) -> None:
        """Flip the finding's status to 'applied' after auto-apply."""
        with _connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE reasoner_findings
                SET status = 'applied', resolved_at = now()
                WHERE rule_id = %s AND finding_key = %s
                """,
                (rule_id, finding_key),
            )
