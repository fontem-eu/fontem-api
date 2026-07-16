"""Unified hybrid search — pgvector cosine + tsvector lexical, RRF fusion.

Backs `GET /api/search/results` from the UI. One Postgres query, all
entity types in one relevance-ranked list, no per-type Cypher fan-out
and no cross-store federation.

Retrieval:
  * dense — linguistics /embed (default minilm-local, 384-dim) → cosine
    over search.entity_embeddings.embedding (HNSW index).
  * sparse — plainto_tsquery('simple', $q) match on the same row's
    name_lex tsvector (GIN index).
  * merge — Reciprocal Rank Fusion, constant 60, weights 1/1.

The 'simple' tsquery config means no per-language stop-word stripping.
Deliberate: we serve 24 EU locales and query language isn't reliably
known. Cost is that lexical ranking treats "the" as a real term; if
that becomes visible as noise we detect language + swap the tsquery
config, or reintroduce a keywords-endpoint call in front. For now,
keep the path simple.

Degradations:
  * linguistics /embed unreachable → falls back to lexical-only. Users
    still get results (no cross-lingual / paraphrase recall) rather
    than a blank page.
  * SEARCH_DATABASE_URL unset → 503 with an operator-visible message.

Response shape mirrors what the SearchView Vue component reads: each
result has `title` (parsed from embed_text[0]) + `subtitle` (embed_text[1])
+ `context` (remainder) + `type`, `id`, `country`, `date`, `meta`.
`counts` groups the returned page by type (drives the facet sidebar);
`has_more` is a peek at (limit+1) rows.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Annotated, Any

import psycopg
from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query

from src.data.linguistics.client import LinguisticsClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


# LIMIT is +1 so we can tell has_more without a second COUNT.
_SQL_HYBRID = """
WITH lex AS (
  SELECT entity_type, entity_id, embed_text, country, event_date,
         ROW_NUMBER() OVER (
           ORDER BY ts_rank(name_lex, plainto_tsquery('simple', %(q)s)) DESC
         ) AS rk
  FROM search.entity_embeddings
  WHERE name_lex @@ plainto_tsquery('simple', %(q)s)
    AND (%(country)s::text IS NULL OR country = %(country)s)
    AND (%(types)s::text[] IS NULL OR entity_type = ANY(%(types)s))
    AND (%(date_from)s::date IS NULL OR event_date >= %(date_from)s::date)
    AND (%(date_to)s::date   IS NULL OR event_date <= %(date_to)s::date)
  ORDER BY rk
  LIMIT 100
),
vec AS (
  SELECT entity_type, entity_id, embed_text, country, event_date,
         ROW_NUMBER() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rk
  FROM search.entity_embeddings
  WHERE encoder_id = %(enc)s
    AND (%(country)s::text IS NULL OR country = %(country)s)
    AND (%(types)s::text[] IS NULL OR entity_type = ANY(%(types)s))
    AND (%(date_from)s::date IS NULL OR event_date >= %(date_from)s::date)
    AND (%(date_to)s::date   IS NULL OR event_date <= %(date_to)s::date)
  ORDER BY embedding <=> %(qvec)s::vector
  LIMIT 100
)
SELECT
  COALESCE(l.entity_type, v.entity_type) AS entity_type,
  COALESCE(l.entity_id, v.entity_id)     AS entity_id,
  COALESCE(l.embed_text, v.embed_text)   AS embed_text,
  COALESCE(l.country, v.country)         AS country,
  COALESCE(l.event_date, v.event_date)   AS event_date,
  l.rk AS lex_rank,
  v.rk AS vec_rank,
  (
    (CASE WHEN l.rk IS NULL THEN 0 ELSE 1.0 / (60 + l.rk) END) +
    (CASE WHEN v.rk IS NULL THEN 0 ELSE 1.0 / (60 + v.rk) END)
  )::real AS rrf_score
FROM lex l
FULL OUTER JOIN vec v USING (entity_type, entity_id)
ORDER BY rrf_score DESC
LIMIT %(limit_plus_one)s;
"""

_SQL_LEXICAL_ONLY = """
SELECT entity_type, entity_id, embed_text, country, event_date,
       ROW_NUMBER() OVER (
         ORDER BY ts_rank(name_lex, plainto_tsquery('simple', %(q)s)) DESC
       ) AS lex_rank,
       NULL::int AS vec_rank,
       ts_rank(name_lex, plainto_tsquery('simple', %(q)s))::real AS rrf_score
FROM search.entity_embeddings
WHERE name_lex @@ plainto_tsquery('simple', %(q)s)
  AND (%(country)s::text IS NULL OR country = %(country)s)
  AND (%(types)s::text[] IS NULL OR entity_type = ANY(%(types)s))
    AND (%(date_from)s::date IS NULL OR event_date >= %(date_from)s::date)
    AND (%(date_to)s::date   IS NULL OR event_date <= %(date_to)s::date)
ORDER BY rrf_score DESC
LIMIT %(limit_plus_one)s;
"""


def _search_dsn() -> str | None:
    dsn = os.environ.get("SEARCH_DATABASE_URL") or os.environ.get("EVENTS_DATABASE_URL")
    if not dsn:
        return None
    return (dsn.replace("postgresql+asyncpg://", "postgresql://")
               .replace("postgresql+psycopg://", "postgresql://"))


def _vec_literal(v: list[float]) -> str:
    """pgvector accepts a stringified list literal '[0.1,-0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _shape_row(row: dict) -> dict:
    """Map a raw search row to the shape SearchView.vue expects.

    embed_text was composed by fontem-embedding-sink as:
      "<title> — <aliases-or-context> — <more-context...>"
    so title is the first segment, subtitle the second, context the rest.
    Fragile if the composer format ever changes — worth revisiting once
    we add title/subtitle columns to the sink table directly (follow-up).
    """
    text = row.get("embed_text") or ""
    parts = [p.strip() for p in text.split(" — ") if p and p.strip()]
    title = parts[0] if parts else ""
    subtitle = parts[1] if len(parts) > 1 else ""
    context = " · ".join(parts[2:]) if len(parts) > 2 else ""
    date = row.get("event_date")
    return {
        "type": row["entity_type"],
        "id": row["entity_id"],
        "title": title,
        "subtitle": subtitle,
        "context": context,
        "country": row.get("country"),
        "date": date.isoformat() if date and not isinstance(date, str) else date,
        "score": float(row["rrf_score"]),
        "meta": {
            "lex_rank": row.get("lex_rank"),
            "vec_rank": row.get("vec_rank"),
        },
    }


@router.get("/results")
@inject
# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals
def search_results(
    q: Annotated[str, Query(min_length=1, max_length=200)],
    types: Annotated[str | None, Query(max_length=200)] = None,
    country: Annotated[str | None, Query(max_length=3)] = None,
    # Accepted for API-compat with the old endpoint; NUTS-level geo
    # filtering isn't wired yet because the sink doesn't project NUTS
    # onto search.entity_embeddings rows. Silently ignored.
    nuts: Annotated[str | None, Query(max_length=8)] = None,  # noqa: ARG001
    date_from: Annotated[
        str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    date_to: Annotated[
        str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    backend: Annotated[
        str, Query(pattern=r"^(minilm-local|labse-local|mistral-embed)$"),
    ] = "minilm-local",
    *,
    linguistics: FromDishka[LinguisticsClient | None],
) -> dict[str, Any]:
    """Faceted hybrid search across every entity type in one page.

    Returns SearchView-shaped envelope:
      {query, results, counts, has_more, timing_ms, mode, backend}
    """
    del nuts  # accepted for API-compat; NUTS geo isn't wired to entity_embeddings yet
    dsn = _search_dsn()
    if not dsn:
        raise HTTPException(
            status_code=503,
            detail=(
                "search unavailable "
                "(SEARCH_DATABASE_URL / EVENTS_DATABASE_URL unset)"
            ),
        )

    types_arr = [t.strip() for t in (types or "").split(",") if t.strip()] or None
    params: dict[str, Any] = {
        "q": q,
        "country": country,
        "types": types_arr,
        "date_from": date_from,
        "date_to": date_to,
        # over-fetch by 1 to detect has_more without a second COUNT
        "limit_plus_one": offset + limit + 1,
    }

    t0 = time.perf_counter()
    embedded = linguistics.embed(q, backend=backend) if linguistics is not None else None
    t_embed_ms = (time.perf_counter() - t0) * 1000

    if embedded is not None:
        qvec, encoder_id = embedded
        params["qvec"] = _vec_literal(qvec)
        params["enc"] = encoder_id
        sql = _SQL_HYBRID
        mode = "hybrid"
    else:
        logger.warning(
            "linguistics /embed unavailable; falling back to lexical-only for q=%r",
            q[:50],
        )
        sql = _SQL_LEXICAL_ONLY
        mode = "lexical_only"

    t_sql_start = time.perf_counter()
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 5000")
                cur.execute(sql, params)
                cols = [d.name for d in cur.description] if cur.description else []
                raw_rows = cur.fetchall()
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=504, detail="search timeout") from exc
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=500, detail=f"search error: {str(exc)[:200]}",
        ) from exc
    t_sql_ms = (time.perf_counter() - t_sql_start) * 1000

    dict_rows = [dict(zip(cols, r)) for r in raw_rows]
    # over-fetch: the +1 row (if present) means has_more, drop it before slicing
    has_more = len(dict_rows) > offset + limit
    dict_rows = dict_rows[offset:offset + limit]

    # Per-type counts over what we surfaced (drives the facet sidebar).
    counts: dict[str, int] = {}
    for r in dict_rows:
        t = r["entity_type"]
        counts[t] = counts.get(t, 0) + 1

    return {
        "query": q,
        "results": [_shape_row(r) for r in dict_rows],
        "counts": counts,
        "has_more": has_more,
        "mode": mode,
        "backend": backend if mode == "hybrid" else None,
        "timing_ms": {
            "embed": round(t_embed_ms, 1),
            "sql": round(t_sql_ms, 1),
            "total": round(t_embed_ms + t_sql_ms, 1),
        },
    }
