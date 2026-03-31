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
