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


def test_connectedness_endpoint_default_shape():
    """GET /data-quality/connectedness returns the default contract
    shape when no override is supplied — keeps older mocks usable."""
    client = make_test_client(data_quality_source=MockDataQualitySource())
    resp = client.get("/data-quality/connectedness")
    cleanup_dishka()
    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "per_type": [],
        "errors": [],
        "generated_at": None,
        "cache_ttl_seconds": 0,
    }


def test_connectedness_endpoint_with_data():
    """Custom mock returns populated per_type stats; endpoint passes
    them through unchanged (no massaging in the router layer)."""

    class ConnectednessMock(MockDataQualitySource):
        def get_graph_connectedness(self):
            return {
                "per_type": [
                    {
                        "entity_type": "Company",
                        "count": 100,
                        "isolated_count": 40,
                        "isolated_pct": 40.0,
                        "min_degree": 0,
                        "max_degree": 42,
                        "mean_degree": 3.5,
                        "median_degree": 2,
                        "p95_degree": 12,
                        "histogram": [
                            {"bucket": "0", "count": 40},
                            {"bucket": "1", "count": 15},
                            {"bucket": "2-5", "count": 30},
                            {"bucket": "6-10", "count": 10},
                            {"bucket": "11-50", "count": 5},
                            {"bucket": "51-100", "count": 0},
                            {"bucket": "101-500", "count": 0},
                            {"bucket": "500+", "count": 0},
                        ],
                    },
                ],
                "generated_at": "2026-04-21T12:00:00+00:00",
                "cache_ttl_seconds": 3600,
            }

    client = make_test_client(data_quality_source=ConnectednessMock())
    resp = client.get("/data-quality/connectedness")
    cleanup_dishka()
    assert resp.status_code == 200
    data = resp.json()
    assert data["cache_ttl_seconds"] == 3600
    assert len(data["per_type"]) == 1
    company = data["per_type"][0]
    assert company["entity_type"] == "Company"
    assert company["isolated_pct"] == 40.0
    # Histogram always has 8 buckets, in canonical order — the
    # frontend relies on this for chart labels.
    buckets = [b["bucket"] for b in company["histogram"]]
    assert buckets == ["0", "1", "2-5", "6-10", "11-50", "51-100", "101-500", "500+"]


def test_source_freshness_default_shape():
    """GET /data-quality/source-freshness returns the empty default
    when no override is supplied — every mock that doesn't fill it in
    still gets a usable response."""
    client = make_test_client(data_quality_source=MockDataQualitySource())
    resp = client.get("/data-quality/source-freshness")
    cleanup_dishka()
    assert resp.status_code == 200
    assert resp.json() == {"sources": [], "generated_at": None}


def test_source_freshness_with_data():
    """A mock that fills in :DataSource markers exposes them through
    the endpoint exactly as written, with the stale flag honoured."""

    class FreshnessMock(MockDataQualitySource):
        def get_source_freshness(self):
            return {
                "sources": [
                    {
                        "id": "sanctions",
                        "label": "EU consolidated sanctions",
                        "coverage_start": "2026-01-01",
                        "coverage_end": "2026-04-29",
                        "last_loaded": "2026-04-29T07:00:00+00:00",
                        "record_count": 3015,
                        "expected_cadence_hours": 25,
                        "age_hours": 2.5,
                        "stale": False,
                    },
                    {
                        "id": "openfigi",
                        "label": "OpenFIGI ticker enrichment",
                        "coverage_start": None,
                        "coverage_end": None,
                        "last_loaded": "2026-03-01T08:00:00+00:00",
                        "record_count": 12345,
                        "expected_cadence_hours": 200,
                        "age_hours": 1416.0,
                        "stale": True,
                    },
                ],
                "generated_at": "2026-04-29T09:30:00+00:00",
            }

    client = make_test_client(data_quality_source=FreshnessMock())
    resp = client.get("/data-quality/source-freshness")
    cleanup_dishka()
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["sources"]) == 2
    by_id = {s["id"]: s for s in data["sources"]}
    assert by_id["sanctions"]["stale"] is False
    assert by_id["openfigi"]["stale"] is True
    assert data["generated_at"] == "2026-04-29T09:30:00+00:00"
