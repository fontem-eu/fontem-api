"""Read API for the fontem_stats Postgres store.

Two endpoints:

- ``GET /stats/series`` — fetch a time-series for one dataset over one or
  more NUTS codes, optionally filtered by a `dimensions` JSONB selector.
- ``GET /stats/datasets`` — list every catalog row plus its latest
  successful sync timestamp. Used by the DQ dashboard's freshness panel.

The bivariate frontend composes its scatter from two independent calls to
``/stats/series`` and joins on ``geo_code`` client-side. Keeping the
shape simple (one dataset per call) avoids cross-product query design.
"""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.stats_etl.db import StatsDatabase

router = APIRouter(prefix="/stats", tags=["stats"])


def _db() -> StatsDatabase:
    """Lazy connect — module import shouldn't require the env var to exist."""
    if "STATS_DATABASE_URL" not in os.environ:
        raise HTTPException(
            status_code=503,
            detail="stats store unavailable (STATS_DATABASE_URL unset)",
        )
    return StatsDatabase()


@router.get("/datasets")
def list_datasets() -> list[dict[str, Any]]:
    """Catalog rows + last-sync metadata for every enabled dataset."""
    db = _db()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                d.code, d.label, d.theme, d.nuts_levels, d.time_unit,
                d.update_freq::text AS update_freq, d.enabled,
                d.notes,
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
        cols = [desc.name for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@router.get("/series")
def fetch_series(
    dataset: str = Query(..., description="Dataset code, e.g. nama_10r_3gdp"),
    geo: list[str] = Query(
        ..., min_length=1,
        description="One or more NUTS codes; matches geo_code exactly",
    ),
    start: int | None = Query(
        None, description="Inclusive start year (e.g. 2010)",
    ),
    end: int | None = Query(
        None, description="Inclusive end year (e.g. 2024)",
    ),
    dimensions: str | None = Query(
        None,
        description='JSONB filter — e.g. {"sex":"T","age":"Y15-74"}',
    ),
) -> dict[str, Any]:
    db = _db()
    where: list[str] = ["dataset_code = %s", "geo_code = ANY(%s)"]
    params: list[Any] = [dataset, geo]
    if start is not None:
        where.append("time >= make_date(%s, 1, 1)")
        params.append(start)
    if end is not None:
        where.append("time <= make_date(%s, 12, 31)")
        params.append(end)
    if dimensions:
        try:
            dim_filter = json.loads(dimensions)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"invalid dimensions JSON: {exc}",
            ) from exc
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
        LIMIT 100000
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql_query, params)
        rows = [
            {
                "geo_code": r[0], "year": r[1], "time": r[2].isoformat(),
                "dimensions": r[3], "value": r[4], "flags": r[5],
            }
            for r in cur.fetchall()
        ]
    return {
        "dataset": dataset,
        "geo": geo,
        "start": start,
        "end": end,
        "dimensions_filter": dimensions,
        "count": len(rows),
        "data": rows,
    }
