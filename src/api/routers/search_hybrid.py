"""Hybrid semantic + lexical search over search.entity_embeddings.

Single-endpoint replacement candidate for the fan-out /search/results
router: one Postgres query, mixed entity types in one relevance-ranked
list, no per-type Cypher scans.

Retrieval:
  1. dense (vector) — MiniLM-local via linguistics /embed → cosine over pgvector
  2. sparse (lexical) — Postgres tsvector match on the same row

Merge via Reciprocal Rank Fusion (RRF, constant 60). Score decomposes
so a row surfacing in ONE method still ranks (weaker), and a row that
BOTH methods like ranks highest. See fontem-embedding-sink docs +
query/hybrid.sql for the merge formula.

Degradations:
  - linguistics /embed unavailable → falls back to lexical-only
    (partial results, no 500). Loud log; better than a blank page.
  - SEARCH_DATABASE_URL unset → 503 (search index isn't provisioned).

Timing budget target: end-to-end p50 ~50-100ms once corpus warms.
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
LIMIT %(limit)s;
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
ORDER BY rrf_score DESC
LIMIT %(limit)s;
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


@router.get("/hybrid")
@inject
# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals
def search_hybrid(
    q: Annotated[str, Query(min_length=1, max_length=200)],
    country: Annotated[str | None, Query(max_length=3)] = None,
    types: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    backend: Annotated[
        str, Query(pattern=r"^(minilm-local|labse-local|mistral-embed)$"),
    ] = "minilm-local",
    *,
    linguistics: FromDishka[LinguisticsClient | None],
) -> dict[str, Any]:
    """Hybrid semantic + lexical search across all entity types.

    Returns a flat, RRF-ranked page mixing companies, authorities,
    contracts, cohesion / lobbying disclosures, sanctioned entities,
    petitions, and investment funds — whichever surface. Each row
    reports its lexical rank, vector rank, and fused RRF score for
    debuggability.
    """
    dsn = _search_dsn()
    if not dsn:
        raise HTTPException(
            status_code=503,
            detail="hybrid search unavailable (SEARCH_DATABASE_URL / EVENTS_DATABASE_URL unset)",
        )

    types_arr = [t.strip() for t in (types or "").split(",") if t.strip()] or None
    params: dict[str, Any] = {
        "q": q,
        "country": country,
        "types": types_arr,
        "limit": limit,
    }

    t_start = time.perf_counter()
    embedded = None
    if linguistics is not None:
        embedded = linguistics.embed(q, backend=backend)
    t_embed_ms = (time.perf_counter() - t_start) * 1000

    if embedded is not None:
        qvec, encoder_id = embedded
        params["qvec"] = _vec_literal(qvec)
        params["enc"] = encoder_id
        sql = _SQL_HYBRID
        mode = "hybrid"
    else:
        # Degrade to lexical-only. Users still get results (worse recall
        # on paraphrase/multilingual queries, but functional).
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
                rows = cur.fetchall()
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=504, detail="search timeout") from exc
    except psycopg.Error as exc:
        raise HTTPException(status_code=500, detail=f"search error: {str(exc)[:200]}") from exc
    t_sql_ms = (time.perf_counter() - t_sql_start) * 1000

    results = [dict(zip(cols, r)) for r in rows]
    # date is a datetime.date — jsonify it
    for r in results:
        d = r.get("event_date")
        if d is not None and not isinstance(d, str):
            r["event_date"] = d.isoformat()

    return {
        "query": q,
        "mode": mode,
        "backend": backend if mode == "hybrid" else None,
        "timing_ms": {"embed": round(t_embed_ms, 1), "sql": round(t_sql_ms, 1),
                      "total": round(t_embed_ms + t_sql_ms, 1)},
        "results": results,
        "count": len(results),
    }
