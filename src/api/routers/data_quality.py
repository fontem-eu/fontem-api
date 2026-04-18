"""
Data Quality API Router
========================
Endpoints for the platform health and data quality dashboard.
"""
from __future__ import annotations

from dishka.integrations.fastapi import FromDishka, inject
from src.analysis.data_quality_source import DataQualitySource

from fastapi import APIRouter, Depends


router = APIRouter(prefix="/data-quality", tags=["data-quality"])


@router.get("")
@inject
def data_quality_overview(
    source: FromDishka[DataQualitySource],
):
    """Full data quality overview for the dashboard."""
    return {
        "graph": source.get_graph_stats(),
        "matching": source.get_matching_stats(),
        "freshness": source.get_data_freshness(),
        "coverage": source.get_coverage_stats(),
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


@router.get("/firds")
@inject
def firds_stats(source: FromDishka[DataQualitySource]):
    """FIRDS instrument data stats."""
    return source.get_firds_stats()


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
def connectedness(source: FromDishka[DataQualitySource]):
    """Graph connectedness: degree distribution, summary stats, top hubs."""
    return source.get_connectedness()
