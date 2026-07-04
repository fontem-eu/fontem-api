"""
Data Quality API Router
========================
Endpoints for the platform health and data quality dashboard.
"""
from __future__ import annotations

import logging
import os

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter

from src.analysis.data_quality_source import DataQualitySource
from src.data_quality import price_index

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data-quality", tags=["data-quality"])


def _safe(label, fn):
    """Run one dashboard section, degrading gracefully on failure.

    The overview fans out ~20 Cypher queries against a large, occasionally
    flaky graph. Without this guard a single transient Neo4j error (timeout,
    ServiceUnavailable, a procedure/license blip) raised straight through and
    500'd the *entire* dashboard. Now a failing section returns an error marker
    and the rest of the dashboard still renders.
    """
    try:
        return fn()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("data-quality section %r failed", label)
        return {"error": "unavailable"}


@router.get("")
@inject
def data_quality_overview(
    source: FromDishka[DataQualitySource],
):
    """Full data quality overview for the dashboard."""
    return {
        "graph": _safe("graph", source.get_graph_stats),
        "matching": _safe("matching", source.get_matching_stats),
        "freshness": _safe("freshness", source.get_data_freshness),
        "coverage": _safe("coverage", source.get_coverage_stats),
    }


@router.get("/graph")
@inject
def graph_stats(source: FromDishka[DataQualitySource]):
    """Node and relationship counts."""
    return source.get_graph_stats()


@router.get("/matching")
@inject
def matching_stats(source: FromDishka[DataQualitySource]):
    """Entity resolution and matching metrics."""
    return source.get_matching_stats()


@router.get("/freshness")
@inject
def freshness_stats(source: FromDishka[DataQualitySource]):
    """Data freshness and loading timestamps."""
    return source.get_data_freshness()


@router.get("/coverage")
@inject
def coverage_stats(source: FromDishka[DataQualitySource]):
    """Data coverage by country, sector, and entity type."""
    return source.get_coverage_stats()


# ── Per-pipeline endpoints ────────────────────────────────────────

@router.get("/contracts/timeline")
@inject
def contracts_timeline(source: FromDishka[DataQualitySource]):
    """Daily contract counts by publication_date."""
    return source.get_contracts_timeline()


@router.get("/contracts/by-country")
@inject
def contracts_by_country(source: FromDishka[DataQualitySource]):
    """Contracts and EUR by country."""
    return source.get_contracts_by_country()


@router.get("/contracts/nulls")
@inject
def contracts_nulls(source: FromDishka[DataQualitySource]):
    """Missing field counts for contracts."""
    return source.get_contracts_nulls()


@router.get("/contracts/currency-quality")
@inject
def contracts_currency_quality(source: FromDishka[DataQualitySource]):
    """Currency-related data quality: undisclosed, inferred, conversion success."""
    return source.get_contracts_currency_quality()


@router.get("/contracts/value-timeline")
@inject
def contracts_value_timeline(source: FromDishka[DataQualitySource]):
    """Daily total EUR value of contracts."""
    return source.get_contracts_value_timeline()


@router.get("/contracts/integrity")
@inject
def contracts_integrity(source: FromDishka[DataQualitySource]):
    """Tender-integrity metrics: bidder-count / procedure-type coverage,
    single-bidder rate, and the red-flag distribution."""
    return source.get_contracts_integrity()


@router.get("/contracts/value-quality")
@inject
def contracts_value_quality(source: FromDishka[DataQualitySource]):
    """Value-confidence overview: how many contracts are excluded from
    value aggregates, the breakdown by quality flag, and the top flagged
    contracts for review."""
    return source.get_contracts_value_quality()


@router.get("/gleif")
@inject
def gleif_stats(source: FromDishka[DataQualitySource]):
    """GLEIF company and relationship stats."""
    return source.get_gleif_stats()


@router.get("/edgar")
@inject
def edgar_stats(source: FromDishka[DataQualitySource]):
    """US EDGAR financial data stats."""
    return source.get_edgar_stats()


@router.get("/esef")
@inject
def esef_stats(source: FromDishka[DataQualitySource]):
    """EU ESEF financial data stats."""
    return source.get_esef_stats()


@router.get("/lobbying")
@inject
def lobbying_stats(source: FromDishka[DataQualitySource]):
    """EU Transparency Register stats."""
    return source.get_lobbying_stats()


@router.get("/trade-edges")
@inject
def trade_edges_stats(source: FromDishka[DataQualitySource]):
    """Materialized trade edge stats."""
    return source.get_trade_edges_stats()


@router.get("/dedup")
@inject
def dedup_stats(source: FromDishka[DataQualitySource]):
    """Deduplication queue stats."""
    return source.get_dedup_stats()


@router.get("/sanctions")
@inject
def sanctions_stats(source: FromDishka[DataQualitySource]):
    """Sanctions list stats."""
    return source.get_sanctions_stats()


@router.get("/triples")
@inject
def triples_stats(source: FromDishka[DataQualitySource]):
    """RDF triple-store inventory: total + per-graph counts +
    per-graph class/predicate breakdown. Returns
    ``{"available": false, ...}`` when no Virtuoso is configured
    so the frontend can render a clean empty state instead of a 500.
    """
    return source.get_triples_stats()


@router.get("/firds")
@inject
def firds_stats(source: FromDishka[DataQualitySource]):
    """FIRDS instrument data stats."""
    return source.get_firds_stats()


@router.get("/prices")
def prices_stats():
    """Price-layer freshness: fetcher index vs the graph-exported
    universe. File-based (NFS), no graph round-trip."""
    data_dir = os.environ.get("GMR_PRICE_DATA_DIR", "/edgar-data/prices")
    return price_index.get_price_stats(data_dir)


@router.get("/openfigi")
@inject
def openfigi_stats(source: FromDishka[DataQualitySource]):
    """OpenFIGI enrichment stats."""
    return source.get_openfigi_stats()


@router.get("/cdp")
@inject
def cdp_stats(source: FromDishka[DataQualitySource]):
    """CDP climate disclosure stats."""
    return source.get_cdp_stats()


@router.get("/nuts")
@inject
def nuts_stats(source: FromDishka[DataQualitySource]):
    """NUTS region stats."""
    return source.get_nuts_stats()


@router.get("/eu-knowledge-graph")
@inject
def eu_knowledge_graph_stats(source: FromDishka[DataQualitySource]):
    """EU Knowledge Graph cohesion project stats."""
    return source.get_eu_knowledge_graph_stats()


@router.get("/cross-source-overlap")
@inject
def cross_source_overlap(source: FromDishka[DataQualitySource]):
    """Cross-source entity overlap counts."""
    return source.get_cross_source_overlap()


@router.get("/country-codes")
@inject
def country_code_consistency(source: FromDishka[DataQualitySource]):
    """Country code format consistency metrics."""
    return source.get_country_code_consistency()


@router.get("/field-completeness")
@inject
def field_completeness(source: FromDishka[DataQualitySource]):
    """Per-source field completeness percentages."""
    return source.get_field_completeness()


@router.get("/connectedness")
@inject
def graph_connectedness(source: FromDishka[DataQualitySource]):
    """Per-label degree stats + histograms. Cached in the source
    layer (1h TTL) because the underlying Cypher is a full scan
    across every entity label."""
    return source.get_graph_connectedness()


@router.get("/eurostat")
def eurostat_freshness():
    """Per-dataset freshness for the Eurostat / regional-stats layer.

    Reads directly from the fontem_stats Postgres store rather than going
    through the dishka-injected source — this layer is in a different
    database than everything else and the wiring is intentionally simple.
    Returns 503 if STATS_DATABASE_URL is unset (e.g., in dev/dast env).
    """
    # Local imports: keep `from src.stats_etl.db import StatsDatabase` out
    # of the module init path — the stats layer is a separately-deployable
    # add-on (different Postgres), so loading it eagerly would force every
    # API instance to carry the stats client even when it's unused.

    if "STATS_DATABASE_URL" not in os.environ:
        from fastapi import HTTPException  # pylint: disable=import-outside-toplevel
        raise HTTPException(
            status_code=503,
            detail="stats store unavailable (STATS_DATABASE_URL unset)",
        )
    from src.stats_etl.db import StatsDatabase  # pylint: disable=import-outside-toplevel
    db = StatsDatabase()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                d.code, d.label, d.theme, d.nuts_levels,
                d.update_freq::text AS update_freq,
                d.enabled,
                r.started_at         AS last_sync_started_at,
                r.upstream_modified  AS last_upstream_modified,
                r.rows_total         AS last_sync_rows,
                EXTRACT(EPOCH FROM (now() - r.started_at))::int AS sync_age_sec
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
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        # Aggregate counters for the dashboard headline.
        cur.execute(
            "SELECT COUNT(*) AS total_obs FROM fontem_stats.observation",
        )
        total_obs = cur.fetchone()[0]
    by_theme: dict[str, list] = {}
    for r in rows:
        by_theme.setdefault(r["theme"], []).append(r)
    enabled = sum(1 for r in rows if r["enabled"])
    fresh = sum(
        1 for r in rows
        if r["sync_age_sec"] is not None and r["sync_age_sec"] < 8 * 86400
    )
    never = sum(1 for r in rows if r["last_sync_started_at"] is None)
    return {
        "total_datasets": len(rows),
        "enabled": enabled,
        "fresh_within_8d": fresh,
        "never_synced": never,
        "total_observations": total_obs,
        "by_theme": by_theme,
    }
