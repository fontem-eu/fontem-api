"""Tests for the data quality API and source."""
from tests.dishka_fixtures import make_test_client, cleanup_dishka
from src.analysis.data_quality_source import DataQualitySource


class MockDataQualitySource(DataQualitySource):
    """Test implementation with fixed data."""

    def get_graph_stats(self):
        return {
            "nodes": {"Company": 100, "Contract": 50, "Authority": 10},
            "relationships": 200,
        }

    def get_matching_stats(self):
        return {
            "same_as_pending": 5,
            "same_as_total": 10,
            "companies_with_vat": 30,
            "companies_with_lei": 80,
            "procurement_only_companies": 20,
        }

    def get_data_freshness(self):
        return {
            "latest_contract_load": "2024-06-15T10:00:00Z",
            "contract_date_range": {
                "earliest": "2024-01-01",
                "latest": "2024-06-15",
            },
            "financial_sources": [
                {"source": "EDGAR", "n": 40},
                {"source": "ESEF", "n": 20},
            ],
        }

    def get_coverage_stats(self):
        return {
            "companies_with_contracts": 25,
            "contracts_by_country": [
                {"country": "DEU", "contracts": 20, "total_value": 1000000},
            ],
            "top_cpv_sectors": [
                {"code": "72000000", "description": "IT services",
                 "contracts": 15, "total_value": 500000},
            ],
            "authority_count": 10,
        }


def test_overview_returns_all_sections():
    """GET /data-quality returns all four sections."""
    client = make_test_client(data_quality_source=MockDataQualitySource())
    resp = client.get("/data-quality")
    cleanup_dishka()
    assert resp.status_code == 200
    data = resp.json()
    assert "graph" in data
    assert "matching" in data
    assert "freshness" in data
    assert "coverage" in data
    assert data["graph"]["nodes"]["Company"] == 100
    assert data["matching"]["same_as_pending"] == 5


def test_graph_stats_endpoint():
    """GET /data-quality/graph returns node counts."""
    client = make_test_client(data_quality_source=MockDataQualitySource())
    resp = client.get("/data-quality/graph")
    cleanup_dishka()
    assert resp.status_code == 200
    assert resp.json()["nodes"]["Contract"] == 50


def test_coverage_endpoint():
    """GET /data-quality/coverage returns country + sector breakdown."""
    client = make_test_client(data_quality_source=MockDataQualitySource())
    resp = client.get("/data-quality/coverage")
    cleanup_dishka()
    assert resp.status_code == 200
    data = resp.json()
    assert data["companies_with_contracts"] == 25
    assert len(data["contracts_by_country"]) == 1
    assert data["contracts_by_country"][0]["country"] == "DEU"


class MockConnectednessSource(MockDataQualitySource):
    """Adds a deterministic connectedness fixture to the mock."""

    def get_connectedness(self):
        return {
            "stats": {
                "total_nodes": 100,
                "total_edges": 80,
                "orphan_count": 40,
                "mean_degree": 0.8,
                "median_degree": 1.0,
                "max_degree": 20,
            },
            "distribution": [
                {"bucket": 0, "label": "0", "nodes": 40},
                {"bucket": 1, "label": "1", "nodes": 35},
                {"bucket": 3, "label": "2-3", "nodes": 15},
                {"bucket": 10, "label": "4-10", "nodes": 8},
                {"bucket": 30, "label": "11-30", "nodes": 2},
                {"bucket": 100, "label": "31-100", "nodes": 0},
                {"bucket": 300, "label": "101-300", "nodes": 0},
                {"bucket": 1000, "label": "301-1000", "nodes": 0},
                {"bucket": 10000, "label": "1001-10000", "nodes": 0},
                {"bucket": 999999, "label": "10000+", "nodes": 0},
            ],
            "hubs": [
                {"labels": ["NUTSRegion"], "id": "Italia", "degree": 20},
            ],
        }


def test_connectedness_endpoint():
    """GET /data-quality/connectedness returns stats + distribution + hubs."""
    client = make_test_client(data_quality_source=MockConnectednessSource())
    resp = client.get("/data-quality/connectedness")
    cleanup_dishka()
    assert resp.status_code == 200
    data = resp.json()
    assert data["stats"]["total_nodes"] == 100
    assert data["stats"]["orphan_count"] == 40
    assert data["stats"]["median_degree"] == 1.0
    assert len(data["distribution"]) == 10
    assert data["distribution"][0]["bucket"] == 0
    assert data["distribution"][0]["nodes"] == 40
    assert data["hubs"][0]["id"] == "Italia"
