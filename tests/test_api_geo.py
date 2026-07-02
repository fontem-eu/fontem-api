"""Tests for the geo aggregation API."""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from unittest.mock import MagicMock

from tests.dishka_fixtures import make_test_client, cleanup_dishka


def _mock_geo_source(rows, entity_rows=None):
    source = MagicMock()
    source.aggregate_by_nuts = MagicMock(return_value=rows)
    source.aggregate_entity_by_nuts = MagicMock(return_value=entity_rows or [])
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


# ── /geo/entity/{entity_id}/aggregate ─────────────────────────


def test_entity_aggregate_returns_200_with_regions():
    entity_rows = [
        {"nuts_code": "DE", "label": "Deutschland", "level": 0, "value": 30},
    ]
    source = _mock_geo_source([], entity_rows=entity_rows)
    client = make_test_client(geo_source=source)
    try:
        r = client.get("/geo/entity/some-gmr-id/aggregate")
        assert r.status_code == 200
        data = r.json()
        assert data["entity_id"] == "some-gmr-id"
        assert data["level"] == 0
        assert data["metric"] == "contracts"
        assert data["regions"] == entity_rows
    finally:
        cleanup_dishka()


def test_entity_aggregate_passes_params():
    source = _mock_geo_source([])
    client = make_test_client(geo_source=source)
    try:
        client.get(
            "/geo/entity/abc-123/aggregate"
            "?level=1&metric=contracts_eur&scope_nuts=DE"
        )
        source.aggregate_entity_by_nuts.assert_called_once_with(
            entity_id="abc-123",
            level=1,
            metric="contracts_eur",
            scope_nuts="DE",
        )
    finally:
        cleanup_dishka()


def test_entity_aggregate_bad_level_returns_422():
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        r = client.get("/geo/entity/abc/aggregate?level=9")
        assert r.status_code == 422
    finally:
        cleanup_dishka()


def test_entity_aggregate_value_error_becomes_400():
    source = MagicMock()
    source.aggregate_by_nuts = MagicMock(return_value=[])
    source.aggregate_entity_by_nuts.side_effect = ValueError("bad metric")
    client = make_test_client(geo_source=source)
    try:
        r = client.get("/geo/entity/abc/aggregate?metric=bogus")
        assert r.status_code == 400
        assert "bad metric" in r.json()["detail"]
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
        # Every feature carries the country alpha-3 (the platform's canonical
        # country key) so alpha-3 datasets can join, e.g. UK -> GBR, EL -> GRC.
        codes = {f["properties"]["nuts_code"]: f["properties"].get("country_a3")
                 for f in data["features"]}
        assert all(v for v in codes.values())        # every feature has one
        assert all(len(v) == 3 for v in codes.values())
        if "UK" in codes:
            assert codes["UK"] == "GBR"
        if "EL" in codes:
            assert codes["EL"] == "GRC"
    finally:
        cleanup_dishka()


def test_nuts_boundaries_level_3_returns_feature_collection():
    """NUTS 3 GeoJSON is now bundled — endpoint must return 200."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        r = client.get("/geo/nuts-boundaries?level=3")
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) >= 1000
    finally:
        cleanup_dishka()
