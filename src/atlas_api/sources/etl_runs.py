"""Read methods against the events.etl_run table.

Each ETL CronJob invocation writes one row here via
`fontem_events.RunLog`. This source surfaces those rows to the
data-quality dashboard.

Lives in `atlas_api/sources/` (alongside fontem_stats) for the same
reason: every SQL string touching the events store is concentrated
here so the API can be moved into its own service later by vendoring
this directory only.
"""
# `_connect()` is @contextmanager-wrapped; pylint mis-infers the generator
# return type as `Class 'value'` and flags `.close()` as a missing member.
# pylint: disable=no-member
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

import psycopg

from src.atlas_api.schemas import SourceHealth


class EtlRunsSource:
    """Read-only repository over events.etl_run."""

    name = "fontem-events-postgres"

    def __init__(self, dsn: str | None) -> None:
        # Mirror FontemStatsSource: strip the asyncpg dialect if it's
        # baked into the URL — psycopg (sync) doesn't speak it.
        normalised = (
            dsn.replace("postgresql+asyncpg://", "postgresql://") if dsn else None
        )
        # Same `$(VAR)` unsubstituted-env detection as
        # FontemStatsSource — if Kubernetes ordering puts the URL var
        # before the password var, libpq sees the literal `$(VAR)`
        # and 28P01s. Fail clean rather than passing it to libpq.
        if normalised and "$(" in normalised:
            self._dsn = None
            self._unsubstituted = True
        else:
            self._dsn = normalised
            self._unsubstituted = False

    @property
    def configured(self) -> bool:
        return self._dsn is not None

    @contextmanager
    def _connect(self):
        if not self._dsn:
            raise RuntimeError("EVENTS_DATABASE_URL not set")
        conn = psycopg.connect(self._dsn, connect_timeout=5)
        try:
            yield conn
        finally:
            conn.close()

    def health(self) -> SourceHealth:
        """Aggregator iterates `atlas_sources` — same shape as
        FontemStatsSource: report `unconfigured` when no DSN,
        `down` on libpq errors, `ok` otherwise.
        """
        if not self.configured:
            if self._unsubstituted:
                return SourceHealth(
                    name=self.name,
                    status="unconfigured",
                    detail=(
                        "EVENTS_DATABASE_URL contains an unsubstituted "
                        "$(VAR) reference — reorder the env list so "
                        "the password var comes first."
                    ),
                )
            return SourceHealth(
                name=self.name,
                status="unconfigured",
                detail="EVENTS_DATABASE_URL is not set on this pod",
            )
        started = time.perf_counter()
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except psycopg.OperationalError as exc:
            return SourceHealth(
                name=self.name, status="down", detail=str(exc)[:200],
            )
        return SourceHealth(
            name=self.name, status="ok",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    def recent_runs(
        self,
        *,
        limit: int = 50,
        cronjob_name: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Last `limit` rows from events.etl_run, newest first.

        Optional filters narrow to a single cronjob or status — both
        backed by the indexes the table ships with
        (`etl_run_cronjob_started`, `etl_run_status_started`).
        Missing-table → empty list so a pre-bootstrap cluster doesn't
        500 the dashboard.
        """
        where: list[str] = []
        params: list[Any] = []
        if cronjob_name:
            where.append("cronjob_name = %s")
            params.append(cronjob_name)
        if status:
            where.append("status = %s")
            params.append(status)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT run_id, cronjob_name, image_tag,
                           started_at, finished_at,
                           status, summary, error_message
                    FROM events.etl_run
                    {clause}
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    params,
                )
                cols = [c.name for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except psycopg.errors.UndefinedTable:
            return []
