"""Read methods against the fontem_stats Postgres store.

Owns every SQL string that Atlas issues against fontem_stats. Routers
call these methods; they don't write SQL themselves. That keeps the
schema-coupling concentrated here for the day we extract the service
(and want to vendor only the read DAL).
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any

import psycopg

from src.atlas_api.schemas import Observation, SourceHealth


class FontemStatsSource:
    """Read-only repository over the fontem_stats Postgres schema."""

    name = "fontem-stats-postgres"

    def __init__(self, dsn: str | None) -> None:
        # Strip the asyncpg dialect — psycopg3 (sync) doesn't speak it.
        normalised = (
            dsn.replace("postgresql+asyncpg://", "postgresql://") if dsn else None
        )
        # `$(VAR)` survives untouched if the Kubernetes env-var ordering
        # puts the URL before the variable it references — once observed
        # in prod (gmr-api with STATS_DATABASE_URL declared before
        # STATS_POSTGRES_PASSWORD). Detect and fail clean rather than
        # passing the literal to libpq, which 28P01s with the actual
        # username and a useless detail.
        if normalised and "$(" in normalised:
            self._dsn = None
            self._unsubstituted = True
        else:
            self._dsn = normalised
            self._unsubstituted = False

    @property
    def configured(self) -> bool:
        """True when the DSN is set and free of unsubstituted `$(VAR)`s."""
        return self._dsn is not None

    @contextmanager
    def _connect(self):
        """Yield a psycopg connection with a 5s connect timeout."""
        if not self._dsn:
            raise RuntimeError("STATS_DATABASE_URL not set")
        conn = psycopg.connect(self._dsn, connect_timeout=5)
        try:
            yield conn
        finally:
            conn.close()

    # ── Health ───────────────────────────────────────────────────────

    def health(self) -> SourceHealth:
        if not self.configured:
            if self._unsubstituted:
                return SourceHealth(
                    name=self.name,
                    status="unconfigured",
                    detail=(
                        "STATS_DATABASE_URL contains an unsubstituted "
                        "$(VAR) reference — the env-var that the URL "
                        "references is declared after it in the pod "
                        "spec. Reorder the env list so the password "
                        "var comes first."
                    ),
                )
            return SourceHealth(
                name=self.name,
                status="unconfigured",
                detail="STATS_DATABASE_URL is not set on this pod",
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

    # ── Catalog ──────────────────────────────────────────────────────

    def list_datasets(self) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.code, d.label, d.theme, d.nuts_levels, d.time_unit,
                       d.update_freq::text AS update_freq, d.enabled,
                       d.notes, d.dim_ids, d.dim_labels,
                       r.started_at         AS last_sync_started_at,
                       r.upstream_modified  AS last_upstream_modified,
                       r.rows_total         AS last_sync_rows
                FROM fontem_stats.dataset d
                LEFT JOIN LATERAL (
                    SELECT started_at, upstream_modified, rows_total
                    FROM fontem_stats.sync_run
                    WHERE dataset_code = d.code AND status = 'success'
                    ORDER BY started_at DESC LIMIT 1
                ) r ON true
                ORDER BY d.theme, d.code
                """,
            )
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ── Series ───────────────────────────────────────────────────────

    def fetch_series(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        self,
        *,
        dataset: str,
        geo: list[str] | None,
        nuts_level: int | None,
        start: int | None,
        end: int | None,
        dim_filter: dict[str, Any] | None,
        row_limit: int,
    ) -> list[Observation]:
        where: list[str] = ["dataset_code = %s"]
        params: list[Any] = [dataset]
        if geo:
            where.append("geo_code = ANY(%s)")
            params.append(geo)
        if nuts_level is not None:
            where.append("char_length(geo_code) = %s")
            params.append(nuts_level + 2)
        if start is not None:
            where.append("time >= make_date(%s, 1, 1)")
            params.append(start)
        if end is not None:
            where.append("time <= make_date(%s, 12, 31)")
            params.append(end)
        if dim_filter:
            where.append("dimensions @> %s::jsonb")
            params.append(json.dumps(dim_filter))

        sql_query = f"""
            SELECT geo_code,
                   EXTRACT(YEAR FROM time)::int AS year,
                   time,
                   dimensions,
                   value,
                   flags
            FROM fontem_stats.observation
            WHERE {' AND '.join(where)}
            ORDER BY geo_code, time, dimensions
            LIMIT %s
        """
        params.append(row_limit)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql_query, params)
            return [
                Observation(
                    geo_code=r[0], year=r[1], time=r[2],
                    dimensions=r[3], value=r[4], flags=r[5],
                )
                for r in cur.fetchall()
            ]
