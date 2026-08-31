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

    def consumer_lag(self) -> list[dict[str, Any]]:
        """Per-consumer offset lag against the head of the event log.

        `lag` is the number of events a consumer has not yet handled.
        It is the difference between the log head and the consumer's
        committed offset, so it is a queue depth rather than a rate —
        a consumer that is merely slow and one that has stopped both
        show a rising number, and `updated_at` is what separates them.

        Missing-table → empty list, same contract as recent_runs().
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                # One scan for the head rather than a correlated subquery
                # per row: entity_events is ~65M rows and max(seq) is an
                # index-only lookup on the primary key.
                cur.execute("SELECT coalesce(max(seq), 0) FROM events.entity_events")
                head = cur.fetchone()[0]
                cur.execute(
                    """
                    SELECT consumer_name, last_seq, updated_at
                    FROM events.consumer_offsets
                    ORDER BY consumer_name
                    """
                )
                rows = []
                for name, last_seq, updated_at in cur.fetchall():
                    rows.append({
                        "consumer_name": name,
                        "last_seq": last_seq,
                        "head_seq": head,
                        "lag": max(head - last_seq, 0),
                        "updated_at": updated_at,
                    })
                return rows
        except psycopg.errors.UndefinedTable:
            return []

    def recent_runs_by_cronjob(
        self, *, per_job: int = 4,
    ) -> list[dict[str, Any]]:
        """The last `per_job` runs for every cronjob, newest first.

        Not expressible with recent_runs(limit=N): a chatty cronjob
        fills the window and quiet ones vanish from it entirely, which
        is precisely the case the dashboard needs to show. The window
        function gives each cronjob its own slice.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_id, cronjob_name, image_tag,
                           started_at, finished_at,
                           status, summary, error_message
                    FROM (
                        SELECT *, row_number() OVER (
                                   PARTITION BY cronjob_name
                                   ORDER BY started_at DESC
                               ) AS rn
                        FROM events.etl_run
                    ) ranked
                    WHERE rn <= %s
                    ORDER BY cronjob_name, started_at DESC
                    """,
                    (per_job,),
                )
                cols = [c.name for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except psycopg.errors.UndefinedTable:
            return []

    def pipeline_metrics(self) -> dict[str, dict]:
        """Raw per-producer and per-cronjob pipeline metrics for the
        data-quality source-health view.

        Returns ``{"by_producer": {producer: {...}}, "by_cronjob":
        {cronjob: {...}}}``. The caller joins these against the
        DataSource registry (producer ⇄ cronjob ⇄ dashboard) and derives
        dead-letter % / staleness. Three small aggregate queries, all
        index-backed; missing tables → empty dicts so a pre-bootstrap
        cluster renders "no data yet" rather than 500-ing.
        """
        by_producer: dict[str, dict] = {}
        by_cronjob: dict[str, dict] = {}
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT producer,
                           count(*) AS events_total,
                           count(*) FILTER (
                               WHERE ts > now() - interval '30 days'
                           ) AS events_30d,
                           max(ts) AS last_event_at
                    FROM events.entity_events
                    GROUP BY producer
                    """
                )
                for producer, total, recent, last in cur.fetchall():
                    by_producer[producer] = {
                        "events_total": total,
                        "events_30d": recent,
                        "last_event_at": last,
                        "deadletter": 0,
                    }
                # Dead-lettered events attributed back to their producer.
                cur.execute(
                    """
                    SELECT ee.producer, count(*) AS n
                    FROM events.dead_letter dl
                    JOIN events.entity_events ee ON ee.seq = dl.seq
                    GROUP BY ee.producer
                    """
                )
                for producer, n in cur.fetchall():
                    by_producer.setdefault(producer, {
                        "events_total": 0, "events_30d": 0,
                        "last_event_at": None, "deadletter": 0,
                    })["deadletter"] = n
                # Latest run per cronjob (DISTINCT ON, index-backed).
                cur.execute(
                    """
                    SELECT DISTINCT ON (cronjob_name)
                           cronjob_name, started_at, finished_at,
                           status, summary
                    FROM events.etl_run
                    ORDER BY cronjob_name, started_at DESC
                    """
                )
                for name, started, finished, status, summary in cur.fetchall():
                    by_cronjob[name] = {
                        "last_run_at": started,
                        "last_run_finished_at": finished,
                        "last_run_status": status,
                        "last_run_summary": summary,
                    }
        except psycopg.errors.UndefinedTable:
            return {"by_producer": {}, "by_cronjob": {}}
        return {"by_producer": by_producer, "by_cronjob": by_cronjob}

    def events_timeline(
        self, producer: str, *, days: int = 90,
    ) -> list[dict[str, Any]]:
        """Events emitted per day by one producer over the last ``days``.

        Powers the per-dashboard "volume over time" panel. Returns
        ``[{"day": date, "events": n}, ...]`` oldest-first; an empty list
        when the table is missing. Bounded by the day window + the
        producer index, so it stays cheap on the 14M-row event log.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT date_trunc('day', ts)::date AS day, count(*) AS n
                    FROM events.entity_events
                    WHERE producer = %s
                      AND ts > now() - make_interval(days => %s)
                    GROUP BY 1
                    ORDER BY 1
                    """,
                    (producer, days),
                )
                return [{"day": day, "events": n} for day, n in cur.fetchall()]
        except psycopg.errors.UndefinedTable:
            return []
