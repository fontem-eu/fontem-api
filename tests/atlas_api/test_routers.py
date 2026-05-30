"""Tests for the Atlas API routers.

Patches the FontemStatsSource methods so the routers exercise their
contracts (validation, status codes, response shape) without needing
a live Postgres. Source-level SQL is exercised separately if/when we
add integration tests.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.atlas_api.app import build_app
from src.atlas_api.schemas import Observation, SourceHealth


def _client(stats_dsn: str | None = "postgresql://test:test@h/d"):
    """Build an isolated standalone app — no shared state with fontem-api."""
    with patch.dict("os.environ", {} if stats_dsn is None
                    else {"STATS_DATABASE_URL": stats_dsn}, clear=False):
        if stats_dsn is None:
            os.environ.pop("STATS_DATABASE_URL", None)
        app = build_app()
    return TestClient(app)


# ── /health ──────────────────────────────────────────────────────────


def test_health_unconfigured_when_no_dsn():
    client = _client(stats_dsn=None)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert any(s["status"] == "unconfigured" for s in body["sources"])


def test_health_ok_when_source_pingable():
    """Atlas-health only rolls up sources the Atlas feature itself uses
    — fontem-stats-postgres at the moment. The events-postgres source
    (which only backs /data-quality/etl-runs) is intentionally excluded:
    bundling it in would make /atlas/health flip to ``degraded`` on envs
    that have Atlas fully wired but happen not to expose the events DB
    to the API pod, which is the trip-wire that used to silently skip
    smoke ATLAS-19/20/21/22.
    """
    fake_stats = SourceHealth(
        name="fontem-stats-postgres", status="ok", latency_ms=2.1,
    )
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.health",
        return_value=fake_stats,
    ):
        r = _client().get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    sources_by_name = {s["name"]: s for s in body["sources"]}
    assert sources_by_name["fontem-stats-postgres"]["latency_ms"] == 2.1
    assert "fontem-events-postgres" not in sources_by_name


def test_health_stays_ok_when_events_db_unset():
    """Concrete regression: /atlas/health must NOT flip to 'degraded'
    just because EVENTS_DATABASE_URL is absent. That env var only
    affects /data-quality/etl-runs.
    """
    fake_stats = SourceHealth(
        name="fontem-stats-postgres", status="ok", latency_ms=2.1,
    )
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.health",
        return_value=fake_stats,
    ):
        # Build a client WITHOUT EVENTS_DATABASE_URL set; atlas-health
        # must still report 'ok' because the stats source is fine.
        with patch.dict(
            "os.environ",
            {"STATS_DATABASE_URL": "postgresql://test:test@h/d"},
            clear=False,
        ):
            os.environ.pop("EVENTS_DATABASE_URL", None)
            app = build_app()
        r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── /datasets ────────────────────────────────────────────────────────


def test_datasets_503_when_unconfigured():
    r = _client(stats_dsn=None).get("/datasets")
    assert r.status_code == 503


def test_datasets_returns_summary():
    rows = [
        {
            "code": "nama_10r_2gdp", "label": "GDP × NUTS-2", "theme": "economy",
            "nuts_levels": [2], "time_unit": "year", "update_freq": "1 year",
            "enabled": True, "notes": None,
            "last_sync_started_at": None, "last_upstream_modified": None,
            "last_sync_rows": None,
            "max_availability_pct": 0.93,
        },
    ]
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.list_datasets",
        return_value=rows,
    ):
        r = _client().get("/datasets")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["code"] == "nama_10r_2gdp"
    # No slice stats yet → response must still validate, not 500.
    assert body[0]["slice_stats"] == []
    # Availability summary is surfaced so the picker can hide
    # low-coverage datasets without a per-row round-trip.
    assert body[0]["max_availability_pct"] == 0.93


def test_datasets_returns_summary_includes_dataset_stats():
    """Per-dataset aggregate range (value_min/max/p02/p50/p98 +
    time_min/max + observation_count + value_kind) must round-trip
    through the catalog response so the Atlas UI can show the
    dataset-wide range at a glance and pin a stable colour scale
    for "view this dataset across years".
    """
    rows = [
        {
            "code": "nama_10r_2gdp", "label": "GDP × NUTS-2", "theme": "economy",
            "nuts_levels": [2], "time_unit": "year", "update_freq": "1 year",
            "enabled": True, "notes": None,
            "last_sync_started_at": None, "last_upstream_modified": None,
            "last_sync_rows": None,
            "max_availability_pct": 0.93,
            "value_min": 0.0, "value_max": 125_000.0,
            "value_p02": 1_200.0, "value_p50": 18_000.0, "value_p98": 92_000.0,
            "observation_count": 42_000,
            "time_min": "2000-01-01T00:00:00+00:00",
            "time_max": "2024-01-01T00:00:00+00:00",
            "value_kind": "sequential",
        },
    ]
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.list_datasets",
        return_value=rows,
    ):
        r = _client().get("/datasets")
    assert r.status_code == 200
    body = r.json()[0]
    assert body["value_min"] == 0.0
    assert body["value_max"] == 125_000.0
    assert body["value_p50"] == 18_000.0
    assert body["observation_count"] == 42_000
    assert body["value_kind"] == "sequential"
    assert body["time_min"].startswith("2000-01-01")
    assert body["time_max"].startswith("2024-01-01")


def test_datasets_returns_summary_when_dataset_stats_missing():
    """`dataset_stats` row missing (pre-backfill cluster or zero-obs
    dataset) → every aggregate field is None. Picker must still
    render and frontend falls back to per-data bounds.
    """
    rows = [
        {
            "code": "demo_zero", "label": "Empty", "theme": "economy",
            "nuts_levels": [2], "time_unit": "year", "update_freq": "1 year",
            "enabled": True, "notes": None,
            "last_sync_started_at": None, "last_upstream_modified": None,
            "last_sync_rows": None,
            "max_availability_pct": None,
            "value_min": None, "value_max": None,
            "value_p02": None, "value_p50": None, "value_p98": None,
            "observation_count": None,
            "time_min": None, "time_max": None,
            "value_kind": None,
        },
    ]
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.list_datasets",
        return_value=rows,
    ):
        r = _client().get("/datasets")
    assert r.status_code == 200
    body = r.json()[0]
    assert body["value_min"] is None
    assert body["observation_count"] is None
    assert body["value_kind"] is None


def test_datasets_returns_summary_when_availability_missing():
    """`max_availability_pct=None` is the pre-backfill state — the
    dataset picker must still render and the toggle simply no-ops.
    """
    rows = [
        {
            "code": "demo_legacy", "label": "Legacy", "theme": "economy",
            "nuts_levels": [2], "time_unit": "year", "update_freq": "1 year",
            "enabled": True, "notes": None,
            "last_sync_started_at": None, "last_upstream_modified": None,
            "last_sync_rows": None,
            "max_availability_pct": None,
        },
    ]
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.list_datasets",
        return_value=rows,
    ):
        r = _client().get("/datasets")
    assert r.status_code == 200
    assert r.json()[0]["max_availability_pct"] is None


def test_slice_stats_endpoint_returns_per_dataset_distribution():
    """Slice stats are fetched lazily per-dataset to keep
    /datasets small (the catalog payload was 57 MB when the stats
    were embedded inline). Pin the dedicated endpoint's shape so
    the frontend's per-dataset fetch keeps working.
    """
    slices = [
        {
            "dimensions": {"iccs": "ICCS0101", "unit": "NR"},
            "value_min": 0.0, "value_max": 12_345.0,
            "value_p02": 1.0, "value_p50": 80.0, "value_p98": 9_500.0,
            "observation_count": 1024,
            "value_kind": "sequential",
            "skew_ratio": 4.7,
        },
        {
            "dimensions": {"iccs": "ICCS0101", "unit": "P_HTHAB"},
            "value_min": 0.0, "value_max": 18.5,
            "value_p02": 0.1, "value_p50": 1.4, "value_p98": 12.3,
            "observation_count": 1024,
            "value_kind": "sequential",
            "skew_ratio": 2.1,
        },
    ]
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.fetch_slice_stats",
        return_value=slices,
    ):
        r = _client().get("/datasets/crim_off_cat/slice-stats")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    nr = next(s for s in body if s["dimensions"]["unit"] == "NR")
    assert nr["value_p98"] == 9_500.0
    assert nr["value_kind"] == "sequential"


def test_year_availability_endpoint_returns_per_level_year_rows():
    """The Atlas low-coverage filter reads this endpoint to know which
    (level, slice, year) combinations have enough region coverage to
    be worth showing. Pin the response shape so the frontend filter
    keeps working.
    """
    rows = [
        {
            "nuts_level": 2,
            "dimensions": {"unit": "MIO_EUR"},
            "year": 2018,
            "regions_with_value": 240,
            "regions_total": 281,
            "availability_pct": 0.8540,
        },
        {
            "nuts_level": 2,
            "dimensions": {"unit": "MIO_EUR"},
            "year": 2023,
            "regions_with_value": 30,
            "regions_total": 281,
            "availability_pct": 0.1067,
        },
    ]
    with patch(
        "src.atlas_api.sources.fontem_stats."
        "FontemStatsSource.fetch_year_availability",
        return_value=rows,
    ):
        r = _client().get("/datasets/nama_10r_2gdp/availability")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    sparse = next(row for row in body if row["year"] == 2023)
    assert sparse["availability_pct"] < 0.20
    assert sparse["regions_with_value"] == 30


def test_year_availability_empty_when_table_missing():
    """Sidecar table is best-effort — frontend toggles must still
    function (no-op) when the table hasn't been backfilled."""
    with patch(
        "src.atlas_api.sources.fontem_stats."
        "FontemStatsSource.fetch_year_availability",
        return_value=[],
    ):
        r = _client().get("/datasets/anything/availability")
    assert r.status_code == 200
    assert r.json() == []


def test_slice_stats_empty_when_table_missing():
    """Stats endpoint must not 500 if the table hasn't been
    backfilled yet — frontend's fallback path needs []."""
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.fetch_slice_stats",
        return_value=[],
    ):
        r = _client().get("/datasets/anything/slice-stats")
    assert r.status_code == 200
    assert r.json() == []


# ── /series ──────────────────────────────────────────────────────────


def test_series_requires_geo_or_nuts_level():
    r = _client().get("/series?dataset=x")
    assert r.status_code == 400
    assert "geo" in r.json()["detail"].lower()


def test_series_invalid_dimensions_json_400():
    r = _client().get("/series?dataset=x&geo=DE&dimensions=not-json")
    assert r.status_code == 400
    assert "dimensions" in r.json()["detail"].lower()


def test_series_returns_payload():
    obs = [
        Observation(
            geo_code="DE21",
            year=2023,
            time=datetime(2023, 1, 1, tzinfo=timezone.utc),
            dimensions={"unit": "MIO_EUR"},
            value=100.0, flags=None,
        ),
    ]
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.fetch_series",
        return_value=obs,
    ):
        r = _client().get("/series?dataset=nama_10r_2gdp&nuts_level=2")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["truncated"] is False
    assert body["data"][0]["geo_code"] == "DE21"


def test_series_passes_eurostat_flag_arrays_through():
    """Eurostat flag codes ship as a `text[]` Postgres array — every
    /atlas/series response in prod was 500'ing because the response
    schema typed `flags` as a single string. This test pins the
    contract: `["p"]`, `["b","e"]`, and an empty `[]` all serialise
    cleanly through the response model.
    """
    obs = [
        Observation(
            geo_code="DE21", year=2023,
            time=datetime(2023, 1, 1, tzinfo=timezone.utc),
            dimensions={"unit": "MIO_EUR"}, value=100.0,
            flags=["p"],
        ),
        Observation(
            geo_code="FR10", year=2023,
            time=datetime(2023, 1, 1, tzinfo=timezone.utc),
            dimensions={"unit": "MIO_EUR"}, value=200.0,
            flags=["b", "e"],
        ),
        Observation(
            geo_code="IT10", year=2023,
            time=datetime(2023, 1, 1, tzinfo=timezone.utc),
            dimensions={"unit": "MIO_EUR"}, value=300.0,
            flags=[],
        ),
    ]
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.fetch_series",
        return_value=obs,
    ):
        r = _client().get("/series?dataset=nama_10r_2gdp&nuts_level=2")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert rows[0]["flags"] == ["p"]
    assert rows[1]["flags"] == ["b", "e"]
    assert rows[2]["flags"] == []


def test_observation_rejects_legacy_string_flags():
    """We changed the schema from `str | None` to `list[str] | None` —
    pin the new contract so a regression to the old shape fails loudly
    in pytest instead of in prod."""
    with pytest.raises(Exception):
        Observation(
            geo_code="DE21", year=2023,
            time=datetime(2023, 1, 1, tzinfo=timezone.utc),
            dimensions={}, value=1.0, flags="p",
        )


def test_series_truncated_when_at_limit():
    # Build N observations equal to the configured limit so `truncated` flips True.
    one = Observation(
        geo_code="DE21",
        year=2023,
        time=datetime(2023, 1, 1, tzinfo=timezone.utc),
        dimensions={},
        value=1.0, flags=None,
    )
    obs = [one] * 100_000
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.fetch_series",
        return_value=obs,
    ):
        r = _client().get("/series?dataset=x&nuts_level=2")
    assert r.json()["truncated"] is True
