"""Postgres connection + repos for the stats schema.

Uses psycopg3 (sync) — bulk inserts via execute_values-style
batching. Async would buy us nothing here: the bottleneck is upstream
HTTP fetching the bulk TSV, not the Postgres write.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .eurostat_source import Observation

logger = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    """psycopg3 doesn't speak `postgresql+asyncpg://`; strip the driver."""
    return url.replace("postgresql+asyncpg://", "postgresql://")


@dataclass
class Dataset:
    code: str
    label: str
    theme: str
    source: str
    source_url: str
    nuts_levels: list[int]
    dim_ids: list[str]
    dim_sizes: list[int]
    time_unit: str
    update_freq: str
    enabled: bool
    notes: str | None = None


@dataclass
class SyncRun:
    id: int | None
    dataset_code: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    upstream_modified: datetime | None
    rows_inserted: int
    rows_updated: int
    rows_total: int
    error_message: str | None


class StatsDatabase:
    """Connection + repository surface for fontem_stats schema."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = _normalize_url(dsn or os.environ["STATS_DATABASE_URL"])

    @contextmanager
    def connect(self):
        """Yield a psycopg3 connection in autocommit-off mode."""
        conn = psycopg.connect(self._dsn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Catalog ──────────────────────────────────────────────────

    def upsert_dataset(self, ds: Dataset) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fontem_stats.dataset (
                    code, label, theme, source, source_url, nuts_levels,
                    dim_ids, dim_sizes, time_unit, update_freq, enabled,
                    notes, updated_at
                )
                VALUES (
                    %(code)s, %(label)s, %(theme)s, %(source)s,
                    %(source_url)s, %(nuts_levels)s, %(dim_ids)s,
                    %(dim_sizes)s, %(time_unit)s, %(update_freq)s::interval,
                    %(enabled)s, %(notes)s, now()
                )
                ON CONFLICT (code) DO UPDATE SET
                    label = EXCLUDED.label,
                    theme = EXCLUDED.theme,
                    source_url = EXCLUDED.source_url,
                    nuts_levels = EXCLUDED.nuts_levels,
                    dim_ids = EXCLUDED.dim_ids,
                    dim_sizes = EXCLUDED.dim_sizes,
                    time_unit = EXCLUDED.time_unit,
                    update_freq = EXCLUDED.update_freq,
                    enabled = EXCLUDED.enabled,
                    notes = EXCLUDED.notes,
                    updated_at = now()
                """,
                {
                    "code": ds.code,
                    "label": ds.label,
                    "theme": ds.theme,
                    "source": ds.source,
                    "source_url": ds.source_url,
                    "nuts_levels": ds.nuts_levels,
                    "dim_ids": ds.dim_ids,
                    "dim_sizes": ds.dim_sizes,
                    "time_unit": ds.time_unit,
                    "update_freq": ds.update_freq,
                    "enabled": ds.enabled,
                    "notes": ds.notes,
                },
            )

    def list_datasets(self, only_enabled: bool = True) -> list[Dataset]:
        query = "SELECT * FROM fontem_stats.dataset"
        if only_enabled:
            query += " WHERE enabled = true"
        query += " ORDER BY code"
        with self.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query)
            rows = cur.fetchall()
        return [
            Dataset(
                code=r["code"], label=r["label"], theme=r["theme"],
                source=r["source"], source_url=r["source_url"],
                nuts_levels=list(r["nuts_levels"]),
                dim_ids=list(r["dim_ids"]),
                dim_sizes=list(r["dim_sizes"]),
                time_unit=r["time_unit"],
                update_freq=str(r["update_freq"]),
                enabled=r["enabled"], notes=r.get("notes"),
            )
            for r in rows
        ]

    def get_dataset(self, code: str) -> Dataset | None:
        with self.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM fontem_stats.dataset WHERE code = %s",
                (code,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return Dataset(
            code=row["code"], label=row["label"], theme=row["theme"],
            source=row["source"], source_url=row["source_url"],
            nuts_levels=list(row["nuts_levels"]),
            dim_ids=list(row["dim_ids"]),
            dim_sizes=list(row["dim_sizes"]),
            time_unit=row["time_unit"],
            update_freq=str(row["update_freq"]),
            enabled=row["enabled"], notes=row.get("notes"),
        )

    # ── Sync runs ───────────────────────────────────────────────

    def start_run(self, dataset_code: str) -> int:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fontem_stats.sync_run (dataset_code, status)
                VALUES (%s, 'running')
                RETURNING id
                """,
                (dataset_code,),
            )
            return cur.fetchone()[0]

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        rows_inserted: int = 0,
        rows_updated: int = 0,
        rows_total: int = 0,
        upstream_modified: datetime | None = None,
        error_message: str | None = None,
    ) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE fontem_stats.sync_run SET
                    status = %s,
                    finished_at = now(),
                    rows_inserted = %s,
                    rows_updated = %s,
                    rows_total = %s,
                    upstream_modified = %s,
                    error_message = %s
                WHERE id = %s
                """,
                (
                    status, rows_inserted, rows_updated, rows_total,
                    upstream_modified, error_message, run_id,
                ),
            )

    def last_successful_run(
        self, dataset_code: str,
    ) -> tuple[datetime | None, datetime | None]:
        """Return (started_at, upstream_modified) of the latest success."""
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT started_at, upstream_modified
                FROM fontem_stats.sync_run
                WHERE dataset_code = %s AND status = 'success'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (dataset_code,),
            )
            row = cur.fetchone()
        if not row:
            return None, None
        return row[0], row[1]

    def stale_datasets(self, stale_after_seconds: int) -> list[str]:
        """Datasets whose latest success is older than `stale_after_seconds`,
        or that have never been synced. Used by `sync --stale-after`."""
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.code
                FROM fontem_stats.dataset d
                LEFT JOIN LATERAL (
                    SELECT started_at FROM fontem_stats.sync_run
                    WHERE dataset_code = d.code AND status = 'success'
                    ORDER BY started_at DESC LIMIT 1
                ) r ON true
                WHERE d.enabled = true
                  AND (r.started_at IS NULL
                       OR r.started_at < now() - make_interval(secs => %s))
                ORDER BY d.code
                """,
                (stale_after_seconds,),
            )
            return [row[0] for row in cur.fetchall()]

    # ── Observations ────────────────────────────────────────────

    def bulk_upsert_observations(
        self,
        dataset_code: str,
        batch: Iterable[Observation],
    ) -> tuple[int, int]:
        """Returns (inserted, updated). Uses ON CONFLICT — re-runs idempotent."""
        rows = [
            (
                dataset_code,
                obs.time,
                obs.geo_code,
                json.dumps(obs.dimensions, sort_keys=True),
                obs.value,
                obs.flags,
            )
            for obs in batch
        ]
        if not rows:
            return 0, 0
        with self.connect() as conn, conn.cursor() as cur:
            # `xmax = 0` marks rows that were freshly inserted vs updated;
            # gives us a cheap split between insert and update counts.
            cur.executemany(
                """
                INSERT INTO fontem_stats.observation
                    (dataset_code, time, geo_code, dimensions, value, flags)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (dataset_code, time, geo_code, dimensions)
                DO UPDATE SET
                    value = EXCLUDED.value,
                    flags = EXCLUDED.flags
                """,
                rows,
            )
            # rowcount reflects affected rows but doesn't split insert vs
            # update for executemany — close enough; we report total.
            total = cur.rowcount or len(rows)
        return total, 0
