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

from fastapi.testclient import TestClient

from src.atlas_api.app import build_app
from src.atlas_api.schemas import Observation, SourceHealth


def _client(stats_dsn: str | None = "postgresql://test:test@h/d"):
    """Build an isolated standalone app — no shared state with gmr-api."""
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
    fake = SourceHealth(name="fontem-stats-postgres", status="ok", latency_ms=2.1)
    with patch(
        "src.atlas_api.sources.fontem_stats.FontemStatsSource.health",
        return_value=fake,
    ):
        r = _client().get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["sources"][0]["latency_ms"] == 2.1


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
    import pytest
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
