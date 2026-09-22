"""Tests for the geo aggregation API."""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from unittest import mock
from unittest.mock import MagicMock

from tests.dishka_fixtures import make_test_client, cleanup_dishka
from src.data import geo_ip
from src.services.location_service import LocationService


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


# ── /geo/client-region ─────────────────────────────────────────


def test_client_region_returns_shape_and_maps_alpha3_to_nuts0():
    # the conversion the endpoint relies on: alpha-3 -> NUTS alpha-2 (GRC->EL)
    assert LocationService.alpha3_to_alpha2("PRT") == "PT"
    assert LocationService.alpha3_to_alpha2("GRC") == "EL"
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        r = client.get("/geo/client-region")
        assert r.status_code == 200
        body = r.json()
        # no geoip db in tests -> nulls, but the contract shape holds
        assert set(body) == {"country_alpha3", "nuts0"}
        assert r.headers.get("cache-control", "").startswith("no-store")
    finally:
        cleanup_dishka()


def test_client_region_resolves_country_from_forwarded_ip():
    """The geoip database returns ISO alpha-2 (`country.iso_code`).

    The endpoint must normalise that to alpha-3 before converting to a
    NUTS-0 code. Driving it through the real request path with a stubbed
    database is the only way to catch the alpha-2/alpha-3 mix-up: the
    shape assertion above stays green either way, because with no
    database loaded every field is null.
    """
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        with mock.patch.object(geo_ip, "country_for", return_value="PT"):
            r = client.get(
                "/geo/client-region",
                headers={"X-Forwarded-For": "193.136.0.1"},
            )
        assert r.status_code == 200
        assert r.json() == {"country_alpha3": "PRT", "nuts0": "PT"}
    finally:
        cleanup_dishka()


def test_client_region_uses_nuts_code_for_greece():
    """Greece is EL in NUTS, not GR — the region picker keys on NUTS."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        with mock.patch.object(geo_ip, "country_for", return_value="GR"):
            r = client.get(
                "/geo/client-region",
                headers={"X-Forwarded-For": "62.103.0.1"},
            )
        assert r.json() == {"country_alpha3": "GRC", "nuts0": "EL"}
    finally:
        cleanup_dishka()


def test_client_region_is_null_when_no_public_ip_is_visible():
    """Private hops only (our own proxies) must not guess a country."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        r = client.get(
            "/geo/client-region", headers={"X-Forwarded-For": "10.0.0.1"},
        )
        assert r.json() == {"country_alpha3": None, "nuts0": None}
    finally:
        cleanup_dishka()


# ── /geo/nuts-regions ──────────────────────────────────────────


def test_nuts_regions_codes_filter_returns_only_what_was_asked_for():
    """A caller wanting three labels should not receive 1,798 rows.

    The full list is ~91 KB and is rebuilt from the boundary files on
    every request, so a feed card naming the region a contract was
    awarded in asks for its own codes.
    """
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        full = client.get("/geo/nuts-regions").json()["regions"]
        assert len(full) > 3
        some = [r["code"] for r in full[:3]]
        got = client.get(
            f"/geo/nuts-regions?codes={','.join(some)}").json()["regions"]
        assert sorted(r["code"] for r in got) == sorted(some)
    finally:
        cleanup_dishka()


def test_nuts_regions_codes_filter_is_case_insensitive_and_trims():
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        full = client.get("/geo/nuts-regions").json()["regions"]
        code = full[0]["code"]
        got = client.get(
            f"/geo/nuts-regions?codes= {code.lower()} ").json()["regions"]
        assert [r["code"] for r in got] == [code]
    finally:
        cleanup_dishka()


def test_nuts_regions_empty_codes_means_none_not_everything():
    """`codes=` is an explicit empty selection.

    Falling back to the full list there would hand a caller that asked
    for nothing the largest response this endpoint has.
    """
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        assert client.get("/geo/nuts-regions?codes=").json()["regions"] == []
    finally:
        cleanup_dishka()


_ROW_FIELDS = {"code", "name", "level", "name_latn", "name_native", "name_source"}


def test_nuts_regions_without_the_filter_is_unchanged():
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        regions = client.get("/geo/nuts-regions").json()["regions"]
        assert len(regions) > 100
        assert set(regions[0]) == _ROW_FIELDS
    finally:
        cleanup_dishka()


def test_nuts_regions_returns_flat_list_all_levels():
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        r = client.get("/geo/nuts-regions")
        assert r.status_code == 200
        regions = r.json()["regions"]
        assert isinstance(regions, list) and len(regions) >= 30
        # each row is a lightweight name record (no geometry)
        row = regions[0]
        assert set(row) == _ROW_FIELDS
        levels = {x["level"] for x in regions}
        assert 0 in levels
        # a child code is prefixed by its parent (hierarchy derivable client-side)
        codes = {x["code"] for x in regions}
        children = [c for c in codes if len(c) == 3]
        assert any(c[:2] in codes for c in children)
    finally:
        cleanup_dishka()


# ── /geo/nuts-regions: names in the 24 languages ───────────────


def _by_code(client, query=""):
    rows = client.get(f"/geo/nuts-regions{query}").json()["regions"]
    return {r["code"]: r for r in rows}


def test_nuts_regions_are_named_in_the_requested_language():
    """The picker is unusable otherwise.

    Eurostat names EL3 only as "Αττική" and "Attiki", so a reader looking
    for Attica in English, French or Portuguese has nothing to recognise —
    which is the bug this endpoint's gazetteer exists to fix.
    """
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        assert _by_code(client, "?lang=en")["EL3"]["name"] == "Attica Region"
        assert _by_code(client, "?lang=el")["EL3"]["name"] == "Περιφέρεια Αττικής"
        assert "Attique" in _by_code(client, "?lang=fr")["EL3"]["name"]
        assert _by_code(client, "?lang=el")["EL"]["name"] == "Ελλάδα"
        assert _by_code(client, "?lang=pt")["EL"]["name"] == "Grécia"
    finally:
        cleanup_dishka()


def test_nuts_regions_echoes_the_language_it_resolved():
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        assert client.get("/geo/nuts-regions?lang=pt-BR").json()["lang"] == "pt"
        # Not an EU-24 language, and not a language at all: English, not a 422.
        # This endpoint is public and agents send whatever they have.
        assert client.get("/geo/nuts-regions?lang=zz").json()["lang"] == "en"
        assert client.get("/geo/nuts-regions").json()["lang"] == "en"
    finally:
        cleanup_dishka()


def test_nuts_regions_keeps_both_eurostat_names_on_every_row():
    """The transliteration is what makes an untranslated region findable,
    and the national-language name is the one on the official documents."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        rows = _by_code(client, "?lang=en")
        assert rows["EL3"]["name_native"] == "Αττική"
        assert rows["EL3"]["name_latn"] == "Attiki"
        assert rows["EL3"]["name_source"] == "en"
        # No translation for this one — it falls back to the transliteration.
        assert rows["EL303"]["name"] == "Kentrikos Tomeas Athinon"
        assert rows["EL303"]["name_source"] == "latn"
    finally:
        cleanup_dishka()


def test_nuts_regions_prefers_the_national_name_for_its_own_readers():
    """A Greek reader wants Κεντρικός Τομέας Αθηνών, not a transliteration
    of it — the Latin form is a fallback for everyone else."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        row = _by_code(client, "?lang=el")["EL303"]
        assert row["name"] == "Κεντρικός Τομέας Αθηνών"
        assert row["name_source"] == "native"
    finally:
        cleanup_dishka()


def test_nuts_regions_names_are_clean():
    """Eurostat ships trailing and doubled spaces, and two Greek names that
    start with a LATIN capital A — which makes them unfindable in Greek."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        rows = _by_code(client)
        assert rows["EL3"]["name_native"] == "Αττική"          # was "Αττική "
        assert rows["EL30"]["name_native"].startswith("\u0391")  # GREEK ALPHA
        assert rows["EL51"]["name_native"].startswith("\u0391")
        for row in rows.values():
            for field in ("name", "name_latn", "name_native"):
                value = row[field]
                assert value == value.strip() and "  " not in value
    finally:
        cleanup_dishka()


def test_nuts_regions_filter_still_works_with_a_language():
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        rows = _by_code(client, "?codes=EL3,PT1A0&lang=fr")
        assert set(rows) == {"EL3", "PT1A0"}
        assert "Lisbonne" in rows["PT1A0"]["name"]
    finally:
        cleanup_dishka()


def test_the_geo_region_endpoints_are_deprecated_delegates():
    """They are kept only so a deploy where the API rolls before the web app
    does not break the picker, and they delegate rather than keeping a second
    implementation of the same lookup. The public surface is /nuts."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        legacy = client.get("/geo/nuts-regions?codes=EL3&lang=fr").json()["regions"][0]
        current = client.get("/nuts/regions?codes=EL3&lang=fr").json()["regions"][0]
        assert legacy["name"] == current["name"]
        assert legacy["name_latn"] == current["name_latn"]
        spec = client.get("/openapi.json").json()["paths"]
        assert spec["/geo/nuts-regions"]["get"]["deprecated"] is True
        assert spec["/geo/nuts-search-index"]["get"]["deprecated"] is True
        assert not spec["/nuts/regions"]["get"].get("deprecated")
    finally:
        cleanup_dishka()


def test_the_assistant_reaches_regions_through_the_public_surface():
    """One tool per job: the deprecated route no longer advertises itself, so
    the model cannot pick the copy that is going away."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        paths = client.get("/openapi.json").json()["paths"]
        tools = {op["x-agent-tool"]["name"]: path
                 for path, item in paths.items() for op in item.values()
                 if isinstance(op, dict) and "x-agent-tool" in op}
        assert tools["list_nuts_regions"] == "/nuts/regions"
        assert tools["find_nuts_region"] == "/nuts/search"
        assert "x-agent-tool" not in paths["/geo/nuts-regions"]["get"]
    finally:
        cleanup_dishka()


# ── /geo/nuts-search-index ─────────────────────────────────────


def test_search_index_covers_every_region_the_list_offers():
    """A region missing from the index can only be found by the name on
    screen — which is the state this whole change exists to leave behind."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        listed = {r["code"] for r in client.get("/geo/nuts-regions").json()["regions"]}
        terms = client.get("/geo/nuts-search-index").json()["terms"]
        assert listed <= set(terms)
        assert all(terms[c] for c in listed)
    finally:
        cleanup_dishka()


def test_search_index_carries_the_other_languages_names():
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        terms = client.get("/geo/nuts-search-index").json()["terms"]
        assert "attica" in terms["EL3"]
        assert "attiki" in terms["EL3"]
        assert "αττικη" in terms["EL3"]
        # Lisbon's region was renumbered in NUTS 2024, so its translations
        # are inherited from the code it replaced (PT170).
        for form in ("lisboa", "lisbonne", "lissabon"):
            assert form in terms["PT1A0"], form
        # A NUTS 3 unit with no translations at all still answers to the
        # metro region it belongs to.
        assert "athina" in terms["EL303"]
    finally:
        cleanup_dishka()


def test_search_index_is_folded():
    """The web app matches folded query against these verbatim, so an
    unfolded term would simply never match."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        terms = client.get("/geo/nuts-search-index").json()["terms"]
        assert "bergstrasse" in terms["DE715"] and "ß" not in terms["DE715"]
        for value in list(terms.values())[:200]:
            assert value == value.lower()
            assert "  " not in value
    finally:
        cleanup_dishka()


def test_large_geo_payloads_are_compressed():
    """Nothing in front of this app compresses, and these are the largest
    JSON responses it serves: 229 KB of region names and 457 KB of search
    terms, both highly repetitive."""
    client = make_test_client(geo_source=_mock_geo_source([]))
    try:
        for path in ("/geo/nuts-regions", "/geo/nuts-search-index"):
            r = client.get(path, headers={"Accept-Encoding": "gzip"})
            assert r.status_code == 200
            assert r.headers.get("content-encoding") == "gzip", path
            assert r.json()      # and it still decodes
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
