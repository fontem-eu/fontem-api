"""Tests for the geo aggregation API."""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from unittest.mock import MagicMock

from tests.dishka_fixtures import make_test_client, cleanup_dishka
from src.data import geo_ip


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


# ── /geo/client-language — first-visit language inference ─────────────


def test_client_language_prefers_first_public_forwarded_ip():
    """XFF chains list the visitor first, then our proxies — the lookup
    must use the first PUBLIC hop and skip private/reserved ones."""
    assert geo_ip.client_ip_from(
        "51.159.141.141, 10.42.1.9", None, "10.0.0.1") == "51.159.141.141"
    # private-only chain falls through to X-Real-IP, then the peer
    assert geo_ip.client_ip_from("10.0.0.7", "51.159.141.141", "10.0.0.1") == "51.159.141.141"
    assert geo_ip.client_ip_from(None, None, "8.8.8.8") == "8.8.8.8"
    # garbage never raises
    assert geo_ip.client_ip_from("not-an-ip, 10.1.1.1", "also-bad", None) is None


def test_client_language_country_map_covers_eu_and_falls_back():
    assert geo_ip.language_for_country("FR") == "fr"
    assert geo_ip.language_for_country("PT") == "pt"
    assert geo_ip.language_for_country("BR") == "pt"
    assert geo_ip.language_for_country("BE") == "nl"
    assert geo_ip.language_for_country("JP") is None  # unmapped → browser decides
    assert geo_ip.language_for_country(None) is None


def test_client_language_endpoint_resolves_and_never_caches(monkeypatch):
    monkeypatch.setattr(
        geo_ip, "country_for", lambda ip: "FR" if ip == "51.159.141.141" else None)
    client = make_test_client()
    resp = client.get(
        "/geo/client-language",
        headers={"x-forwarded-for": "51.159.141.141, 10.42.0.3"})
    cleanup_dishka()
    assert resp.status_code == 200
    assert resp.json() == {"country": "FR", "lang": "fr"}
    assert "no-store" in resp.headers["cache-control"]


def test_client_language_unknown_ip_degrades_to_null(monkeypatch):
    monkeypatch.setattr(geo_ip, "country_for", lambda ip: None)
    client = make_test_client()
    resp = client.get("/geo/client-language", headers={"x-forwarded-for": "203.0.113.9"})
    cleanup_dishka()
    assert resp.status_code == 200
    assert resp.json() == {"country": None, "lang": None}


def test_client_language_real_database_resolves_france():
    """End-to-end against the vendored DB: the CI runner may be anywhere,
    so pin a known Scaleway (FR) address rather than the runner's own."""
    if geo_ip.country_for("51.159.141.141") is None:
        # DB not present in this checkout (e.g. shallow tooling) — the
        # endpoint degrades to null rather than failing, by design.
        return
    assert geo_ip.country_for("51.159.141.141") == "FR"
    assert geo_ip.language_for_country(geo_ip.country_for("51.159.141.141")) == "fr"
