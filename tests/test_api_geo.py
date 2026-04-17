"""Tests for the geo aggregation API."""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from unittest.mock import MagicMock

from tests.dishka_fixtures import make_test_client, cleanup_dishka


def _mock_geo_source(rows):
    source = MagicMock()
    source.aggregate_by_nuts = MagicMock(return_value=rows)
    return source


# ── /geo/aggregate ─────────────────────────────────────────────


def test_aggregate_default_level_0_companies():
    rows = [
        {"nuts_code": "DE", "label": "Deutschland", "level": 0, "value": 12000},
        {"nuts_code": "FR", "label": "France", "level": 0, "value": 8000},
    ]
    client = make_test_client(geo_source=_mock_geo_source(rows))
    try:
        r = client.get("/geo/aggregate")
        assert r.status_code == 200
        data = r.json()
        assert data["level"] == 0
        assert data["metric"] == "companies"
        assert data["regions"] == rows
    finally:
        cleanup_dishka()


def test_aggregate_passes_filters_through():
    source = _mock_geo_source([])
    client = make_test_client(geo_source=source)
    try:
        client.get(
            "/geo/aggregate?level=2&metric=contracts_eur"
            "&scope_nuts=DE1&connected_to_country=RUS"
        )
        source.aggregate_by_nuts.assert_called_once_with(
            level=2,
            metric="contracts_eur",
            scope_nuts="DE1",
            connected_to_country="RUS",
        )
    finally:
        cleanup_dishka()


def test_aggregate_bad_level_returns_422():
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        r = client.get("/geo/aggregate?level=4")
        assert r.status_code == 422  # FastAPI query validation
    finally:
        cleanup_dishka()


def test_aggregate_source_value_error_becomes_400():
    source = MagicMock()
    source.aggregate_by_nuts.side_effect = ValueError(
        "level=3 requires scope_nuts (a NUTS 1 ancestor)"
    )
    client = make_test_client(geo_source=source)
    try:
        r = client.get("/geo/aggregate?level=3")
        assert r.status_code == 400
        assert "scope_nuts" in r.json()["detail"]
    finally:
        cleanup_dishka()


# ── /geo/nuts-boundaries ───────────────────────────────────────


def test_nuts_boundaries_level_0_returns_feature_collection():
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        r = client.get("/geo/nuts-boundaries?level=0")
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "FeatureCollection"
        assert isinstance(data["features"], list)
        assert len(data["features"]) >= 30
        # Every feature has nuts_code + name
        props = data["features"][0]["properties"]
        assert "nuts_code" in props
        assert "name" in props
    finally:
        cleanup_dishka()


def test_nuts_boundaries_level_3_returns_501_not_yet_bundled():
    """NUTS 3 GeoJSON isn't bundled yet — endpoint must return 501, not 500."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        r = client.get("/geo/nuts-boundaries?level=3")
        assert r.status_code == 501
        assert "not bundled" in r.json()["detail"].lower()
    finally:
        cleanup_dishka()
