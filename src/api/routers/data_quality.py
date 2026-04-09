"""
Data Quality API Router
========================
Endpoints for the platform health and data quality dashboard.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..dependencies import get_data_quality_source

router = APIRouter(prefix="/data-quality", tags=["data-quality"])


@router.get("")
def data_quality_overview(
    source=Depends(get_data_quality_source),
):
    """Full data quality overview for the dashboard."""
    return {
        "graph": source.get_graph_stats(),
        "matching": source.get_matching_stats(),
        "freshness": source.get_data_freshness(),
        "coverage": source.get_coverage_stats(),
    }


@router.get("/graph")
def graph_stats(source=Depends(get_data_quality_source)):
    """Node and relationship counts."""
    return source.get_graph_stats()


@router.get("/matching")
def matching_stats(source=Depends(get_data_quality_source)):
    """Entity resolution and matching metrics."""
    return source.get_matching_stats()


@router.get("/freshness")
def freshness_stats(source=Depends(get_data_quality_source)):
    """Data freshness and loading timestamps."""
    return source.get_data_freshness()


@router.get("/coverage")
def coverage_stats(source=Depends(get_data_quality_source)):
    """Data coverage by country, sector, and entity type."""
    return source.get_coverage_stats()


# ── Per-pipeline endpoints ────────────────────────────────────────

@router.get("/contracts/timeline")
def contracts_timeline(source=Depends(get_data_quality_source)):
    """Daily contract counts by publication_date."""
    return source.get_contracts_timeline()


@router.get("/contracts/by-country")
def contracts_by_country(source=Depends(get_data_quality_source)):
    """Contracts and EUR by country."""
    return source.get_contracts_by_country()


@router.get("/contracts/nulls")
def contracts_nulls(source=Depends(get_data_quality_source)):
    """Missing field counts for contracts."""
    return source.get_contracts_nulls()


@router.get("/contracts/value-timeline")
def contracts_value_timeline(source=Depends(get_data_quality_source)):
    """Daily total EUR value of contracts."""
    return source.get_contracts_value_timeline()


@router.get("/gleif")
def gleif_stats(source=Depends(get_data_quality_source)):
    """GLEIF company and relationship stats."""
    return source.get_gleif_stats()


@router.get("/edgar")
def edgar_stats(source=Depends(get_data_quality_source)):
    """US EDGAR financial data stats."""
    return source.get_edgar_stats()


@router.get("/esef")
def esef_stats(source=Depends(get_data_quality_source)):
    """EU ESEF financial data stats."""
    return source.get_esef_stats()


@router.get("/lobbying")
def lobbying_stats(source=Depends(get_data_quality_source)):
    """EU Transparency Register stats."""
    return source.get_lobbying_stats()


@router.get("/directors")
def directors_stats(source=Depends(get_data_quality_source)):
    """French directors/person data stats."""
    return source.get_directors_stats()


@router.get("/trade-edges")
def trade_edges_stats(source=Depends(get_data_quality_source)):
    """Materialized trade edge stats."""
    return source.get_trade_edges_stats()


@router.get("/dedup")
def dedup_stats(source=Depends(get_data_quality_source)):
    """Deduplication queue stats."""
    return source.get_dedup_stats()
