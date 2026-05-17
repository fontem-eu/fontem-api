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


def _row_to_dataset(r: dict) -> "Dataset":
    """Build a Dataset from a fetched catalog row.

    Tolerant of older catalogs without the `dim_labels` column so a
    pod from a newer image can still read a row that hasn't been
    re-upserted since the migration.
    """
    raw_labels = r.get("dim_labels")
    if isinstance(raw_labels, str):
        try:
            raw_labels = json.loads(raw_labels)
        except json.JSONDecodeError:
            raw_labels = None
    return Dataset(
        code=r["code"], label=r["label"], theme=r["theme"],
        source=r["source"], source_url=r["source_url"],
        nuts_levels=list(r["nuts_levels"]),
        dim_ids=list(r["dim_ids"]),
        dim_sizes=list(r["dim_sizes"]),
        time_unit=r["time_unit"],
        update_freq=str(r["update_freq"]),
        enabled=r["enabled"], notes=r.get("notes"),
        dim_labels=raw_labels or None,
    )


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
    # Per-dim {code → human label} map written at sync time. Empty until
    # the first successful sync for the dataset. Surfaced by the Atlas
    # API so the UI can render labels instead of opaque codes.
    dim_labels: dict[str, dict[str, str]] | None = None


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


@dataclass
class YearAvailability:
    """Per-(dataset, nuts_level, slice, year) coverage row.

    `availability_pct` is `regions_with_value / regions_total`, with the
    denominator being the distinct geo_codes ever observed at that NUTS
    level across the whole stats schema (not just this dataset). That
    gives a level-wide universe estimate without depending on
    `nuts_region` being backfilled. The frontend uses the pct to hide
    sparse years (per-row) and sparse datasets (max pct < threshold).
    """
    dataset_code: str
    nuts_level: int
    slice_key: str
    dimensions: dict[str, object]
    year: int
    regions_with_value: int
    regions_total: int | None
    availability_pct: float | None
    computed_at: datetime | None


@dataclass
class SliceStats:
    """Per-(dataset, dimension-slice) value statistics.

    Powers the Atlas legend + the stable cross-year colour scale —
    the frontend reads `value_p02` / `value_p98` (robust to outliers)
    to decide colour bin breakpoints, and `value_kind` to switch
    between sequential (viridis) and diverging (PuOr) palettes.
    """
    dataset_code: str
    slice_key: str
    dimensions: dict[str, object]
    value_min: float | None
    value_max: float | None
    value_p02: float | None
    value_p50: float | None
    value_p98: float | None
    observation_count: int
    value_kind: str          # 'sequential' | 'diverging'
    skew_ratio: float | None
    computed_at: datetime | None


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
                    -- Don't clobber dim_ids/dim_sizes if the seed has empty
                    -- placeholders (the loader fills them at sync time).
                    dim_ids = COALESCE(NULLIF(EXCLUDED.dim_ids, '{}'),
                                       fontem_stats.dataset.dim_ids),
                    dim_sizes = COALESCE(NULLIF(EXCLUDED.dim_sizes, '{}'),
                                         fontem_stats.dataset.dim_sizes),
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
        return [_row_to_dataset(r) for r in rows]

    def get_dataset(self, code: str) -> Dataset | None:
        with self.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM fontem_stats.dataset WHERE code = %s",
                (code,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return _row_to_dataset(row)

    def update_dataset_metadata(
        self,
        code: str,
        *,
        dim_ids: list[str] | None = None,
        dim_sizes: list[int] | None = None,
        dim_labels: dict[str, dict[str, str]] | None = None,
    ) -> None:
        """Patch the catalog row with metadata from a fresh upstream fetch.

        Called by the loader after `fetch_metadata` so the row reflects
        the latest dim universe + human labels. Only updates fields that
        are passed in.
        """
        sets: list[str] = []
        params: list[object] = []
        if dim_ids is not None:
            sets.append("dim_ids = %s")
            params.append(dim_ids)
        if dim_sizes is not None:
            sets.append("dim_sizes = %s")
            params.append(dim_sizes)
        if dim_labels is not None:
            sets.append("dim_labels = %s::jsonb")
            params.append(json.dumps(dim_labels))
        if not sets:
            return
        sets.append("updated_at = now()")
        params.append(code)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE fontem_stats.dataset SET {', '.join(sets)} WHERE code = %s",
                params,
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

    # ── Slice stats ─────────────────────────────────────────────
    #
    # Per-(dataset_code, dimension_slice) value distribution stats.
    # Recomputed at the tail of every successful sync so the Atlas
    # legend + colour scale stay current. Robust percentiles
    # (p02/p98) are the workhorse — they ignore one or two outlier
    # regions that would otherwise compress the entire ramp into the
    # bottom 5%.

    def migrate_slice_stats(self) -> None:
        """Idempotent CREATE TABLE for the slice-stats sidecar.

        The init SQL declares this too, but the init scripts only
        run on a fresh data directory. Calling this from the loader
        + the API at startup brings already-deployed clusters
        forward without an out-of-band manual ALTER.
        """
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fontem_stats.dataset_slice_stats (
                    dataset_code      text     NOT NULL
                        REFERENCES fontem_stats.dataset(code) ON DELETE CASCADE,
                    slice_key         text     NOT NULL,
                    dimensions        jsonb    NOT NULL DEFAULT '{}'::jsonb,
                    value_min         double precision,
                    value_max         double precision,
                    value_p02         double precision,
                    value_p50         double precision,
                    value_p98         double precision,
                    observation_count bigint   NOT NULL DEFAULT 0,
                    value_kind        text     NOT NULL DEFAULT 'sequential'
                                       CHECK (value_kind IN ('sequential','diverging')),
                    skew_ratio        double precision,
                    computed_at       timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (dataset_code, slice_key)
                )
                """,
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS dataset_slice_stats_dataset_idx
                    ON fontem_stats.dataset_slice_stats (dataset_code)
                """,
            )

    def recompute_slice_stats(self, dataset_code: str) -> int:
        """Recompute slice stats for a single dataset.

        Aggregates `fontem_stats.observation` grouped by `dimensions`
        and upserts into `dataset_slice_stats`. Returns the number of
        slice rows written.

        `slice_key` is `md5(dimensions::text)` — Postgres' jsonb ::text
        cast is canonical-ordered, so the same slice produces the same
        hash regardless of how the loader wrote the keys.
        """
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fontem_stats.dataset_slice_stats (
                    dataset_code, slice_key, dimensions,
                    value_min, value_max, value_p02, value_p50, value_p98,
                    observation_count, value_kind, skew_ratio, computed_at
                )
                SELECT
                    o.dataset_code,
                    md5(o.dimensions::text) AS slice_key,
                    o.dimensions,
                    min(o.value),
                    max(o.value),
                    percentile_cont(0.02) WITHIN GROUP (ORDER BY o.value) AS p02,
                    percentile_cont(0.50) WITHIN GROUP (ORDER BY o.value) AS p50,
                    percentile_cont(0.98) WITHIN GROUP (ORDER BY o.value) AS p98,
                    count(*),
                    CASE
                        WHEN min(o.value) < 0 AND max(o.value) > 0 THEN 'diverging'
                        ELSE 'sequential'
                    END AS value_kind,
                    -- (p98-p50) / (p50-p02) — > 1 means right-skewed; > 5
                    -- is "consider log scale" territory. NULL when the
                    -- divisor is zero (pathological flat distribution).
                    CASE
                        WHEN percentile_cont(0.50) WITHIN GROUP (ORDER BY o.value)
                           - percentile_cont(0.02) WITHIN GROUP (ORDER BY o.value) <= 0
                        THEN NULL
                        ELSE
                            (percentile_cont(0.98) WITHIN GROUP (ORDER BY o.value)
                             - percentile_cont(0.50) WITHIN GROUP (ORDER BY o.value))
                          / NULLIF(
                                percentile_cont(0.50) WITHIN GROUP (ORDER BY o.value)
                              - percentile_cont(0.02) WITHIN GROUP (ORDER BY o.value),
                                0)
                    END AS skew_ratio,
                    now()
                FROM fontem_stats.observation o
                WHERE o.dataset_code = %s AND o.value IS NOT NULL
                GROUP BY o.dataset_code, o.dimensions
                ON CONFLICT (dataset_code, slice_key) DO UPDATE SET
                    dimensions        = EXCLUDED.dimensions,
                    value_min         = EXCLUDED.value_min,
                    value_max         = EXCLUDED.value_max,
                    value_p02         = EXCLUDED.value_p02,
                    value_p50         = EXCLUDED.value_p50,
                    value_p98         = EXCLUDED.value_p98,
                    observation_count = EXCLUDED.observation_count,
                    value_kind        = EXCLUDED.value_kind,
                    skew_ratio        = EXCLUDED.skew_ratio,
                    computed_at       = now()
                """,
                (dataset_code,),
            )
            return cur.rowcount or 0

    # ── Year availability ──────────────────────────────────────
    #
    # Per-(dataset, nuts_level, slice, year) coverage. A year row's
    # `regions_with_value` is the count of distinct geo_codes with a
    # non-null value at that level/slice in that year. The denominator
    # is the level-wide universe — distinct geo_codes ever observed at
    # that NUTS level across the entire stats schema, prefering
    # `nuts_region` when populated. Self-contained (doesn't require
    # NUTS hierarchy backfill before it can run).
    #
    # The Atlas frontend uses these to hide low-coverage years
    # (`availability_pct < threshold`) and low-coverage datasets
    # (best year < threshold).

    def migrate_year_availability(self) -> None:
        """Idempotent CREATE TABLE for the availability sidecar.

        Same rationale as `migrate_slice_stats`: init scripts only
        run on fresh DB volumes, so an explicit migration runs from
        the loader + API at boot to forward-port already-deployed
        clusters.

        Also creates the `level_universe` cache — the level-wide
        denominator that used to be recomputed via a per-dataset
        CTE that full-table-scanned the observation hypertable on
        every recompute. The cache is refreshed once per ETL cycle
        (see `recompute_level_universe`) and read by every
        `recompute_year_availability` call.
        """
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fontem_stats.dataset_year_availability (
                    dataset_code        text     NOT NULL
                        REFERENCES fontem_stats.dataset(code) ON DELETE CASCADE,
                    nuts_level          smallint NOT NULL,
                    slice_key           text     NOT NULL,
                    dimensions          jsonb    NOT NULL DEFAULT '{}'::jsonb,
                    year                smallint NOT NULL,
                    regions_with_value  int      NOT NULL DEFAULT 0,
                    regions_total       int,
                    availability_pct    double precision,
                    computed_at         timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (dataset_code, nuts_level, slice_key, year)
                )
                """,
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS dataset_year_availability_dataset_idx
                    ON fontem_stats.dataset_year_availability (dataset_code)
                """,
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fontem_stats.level_universe (
                    nuts_level  smallint    PRIMARY KEY,
                    n           int         NOT NULL,
                    computed_at timestamptz NOT NULL DEFAULT now()
                )
                """,
            )

    def recompute_level_universe(self) -> int:
        """Refresh the level-wide region count cache.

        The denominator for `dataset_year_availability.availability_pct`
        is the count of distinct regions observable at each NUTS level —
        it doesn't depend on the dataset being recomputed and only
        drifts when new datasets land observations at a new geo_code,
        or when `nuts_region` gets populated.

        Computing it inline as a CTE per-dataset (the pre-2026-05 path)
        full-table-scanned the observation hypertable once per dataset;
        with 40 datasets and ~600 MB of data that was the only
        non-trivial query in the schema (20s mean per `recompute`).

        Call this once per ETL cycle, before the per-dataset
        `recompute_year_availability` loop.
        """
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH fresh AS (
                    SELECT lvl AS nuts_level,
                           COALESCE(nuts_n, observed_n, 0) AS n
                    FROM (
                        SELECT generate_series(0, 3) AS lvl
                    ) levels
                    LEFT JOIN (
                        SELECT level::int AS lvl, COUNT(*)::int AS nuts_n
                        FROM fontem_stats.nuts_region
                        GROUP BY level
                    ) nr USING (lvl)
                    LEFT JOIN (
                        SELECT (char_length(geo_code) - 2)::int AS lvl,
                               COUNT(DISTINCT geo_code)::int    AS observed_n
                        FROM fontem_stats.observation
                        WHERE value IS NOT NULL
                        GROUP BY char_length(geo_code) - 2
                    ) obs USING (lvl)
                )
                INSERT INTO fontem_stats.level_universe (nuts_level, n, computed_at)
                SELECT nuts_level, n, now() FROM fresh
                ON CONFLICT (nuts_level) DO UPDATE
                    SET n = EXCLUDED.n,
                        computed_at = EXCLUDED.computed_at
                """,
            )
            return cur.rowcount or 0

    def recompute_year_availability(self, dataset_code: str) -> int:
        """Recompute year availability for a single dataset.

        Aggregates `fontem_stats.observation` grouped by
        (nuts_level, dimensions, year) and stores the count of
        distinct regions with a non-null value. The denominator
        (level universe) is read from `fontem_stats.level_universe`
        — refresh it once per ETL cycle via
        `recompute_level_universe()` before calling this in a loop.
        """
        with self.connect() as conn, conn.cursor() as cur:
            # Wipe and rewrite in one transaction — the universe (level
            # denominator) drifts as new datasets land, so a stale
            # row could carry an outdated `regions_total`. Cheaper to
            # recompute everything for the dataset than to compute
            # diffs.
            cur.execute(
                "DELETE FROM fontem_stats.dataset_year_availability "
                "WHERE dataset_code = %s",
                (dataset_code,),
            )
            cur.execute(
                """
                WITH per_year AS (
                    SELECT
                        dataset_code,
                        (char_length(geo_code) - 2)::smallint AS nuts_level,
                        md5(dimensions::text)                  AS slice_key,
                        dimensions,
                        EXTRACT(YEAR FROM time)::smallint      AS year,
                        COUNT(DISTINCT geo_code)
                            FILTER (WHERE value IS NOT NULL)::int
                            AS regions_with_value
                    FROM fontem_stats.observation
                    WHERE dataset_code = %s
                    GROUP BY dataset_code,
                             char_length(geo_code) - 2,
                             md5(dimensions::text),
                             dimensions,
                             EXTRACT(YEAR FROM time)
                )
                INSERT INTO fontem_stats.dataset_year_availability (
                    dataset_code, nuts_level, slice_key, dimensions, year,
                    regions_with_value, regions_total,
                    availability_pct, computed_at
                )
                SELECT
                    p.dataset_code, p.nuts_level, p.slice_key,
                    p.dimensions, p.year,
                    p.regions_with_value,
                    u.n AS regions_total,
                    CASE
                        WHEN COALESCE(u.n, 0) = 0 THEN NULL
                        ELSE p.regions_with_value::float / u.n
                    END AS availability_pct,
                    now()
                FROM per_year p
                LEFT JOIN fontem_stats.level_universe u
                    ON u.nuts_level = p.nuts_level
                """,
                (dataset_code,),
            )
            return cur.rowcount or 0

    def list_year_availability(
        self, dataset_code: str,
    ) -> list[YearAvailability]:
        """Return every (nuts_level, slice, year) row for a dataset."""
        with self.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT dataset_code, nuts_level, slice_key, dimensions,
                       year, regions_with_value, regions_total,
                       availability_pct, computed_at
                FROM fontem_stats.dataset_year_availability
                WHERE dataset_code = %s
                ORDER BY nuts_level, slice_key, year
                """,
                (dataset_code,),
            )
            rows = cur.fetchall()
        out: list[YearAvailability] = []
        for r in rows:
            dims = r["dimensions"]
            if isinstance(dims, str):
                try:
                    dims = json.loads(dims)
                except json.JSONDecodeError:
                    dims = {}
            out.append(
                YearAvailability(
                    dataset_code=r["dataset_code"],
                    nuts_level=int(r["nuts_level"]),
                    slice_key=r["slice_key"],
                    dimensions=dims or {},
                    year=int(r["year"]),
                    regions_with_value=int(r["regions_with_value"]),
                    regions_total=(
                        None if r["regions_total"] is None
                        else int(r["regions_total"])
                    ),
                    availability_pct=r["availability_pct"],
                    computed_at=r["computed_at"],
                )
            )
        return out

    def list_slice_stats(self, dataset_code: str) -> list[SliceStats]:
        """Return every (dimensions slice → stats) row for a dataset.

        The Atlas API embeds these into `/datasets` so the frontend
        can pick the right slice's bounds before rendering the
        choropleth — no extra round-trip per render.
        """
        with self.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT dataset_code, slice_key, dimensions,
                       value_min, value_max,
                       value_p02, value_p50, value_p98,
                       observation_count, value_kind, skew_ratio, computed_at
                FROM fontem_stats.dataset_slice_stats
                WHERE dataset_code = %s
                ORDER BY slice_key
                """,
                (dataset_code,),
            )
            rows = cur.fetchall()
        out: list[SliceStats] = []
        for r in rows:
            dims = r["dimensions"]
            if isinstance(dims, str):
                try:
                    dims = json.loads(dims)
                except json.JSONDecodeError:
                    dims = {}
            out.append(
                SliceStats(
                    dataset_code=r["dataset_code"],
                    slice_key=r["slice_key"],
                    dimensions=dims or {},
                    value_min=r["value_min"],
                    value_max=r["value_max"],
                    value_p02=r["value_p02"],
                    value_p50=r["value_p50"],
                    value_p98=r["value_p98"],
                    observation_count=r["observation_count"],
                    value_kind=r["value_kind"],
                    skew_ratio=r["skew_ratio"],
                    computed_at=r["computed_at"],
                )
            )
        return out
