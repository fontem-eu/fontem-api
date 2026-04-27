"""Tests for the /stats router (catalog + series endpoints).

The router talks to the fontem_stats Postgres store via psycopg directly
(not dishka-injected), so these tests stub StatsDatabase with a minimal
fake that captures the SQL + params for assertion.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routers.stats import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class _FakeCursor:
    def __init__(self, rows: list[tuple], cols: list[str]):
        self.rows = rows
        self._cols = cols
        self.last_sql = ""
        self.last_params: list = []

    def execute(self, sql: str, params=None):
        self.last_sql = sql
        self.last_params = params or []

    def fetchall(self):
        return self.rows

    @property
    def description(self):
        return [MagicMock(name=c) for c in self._cols]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cur = cursor

    def cursor(self, **_kw):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _fake_db_factory(rows: list[tuple], cols: list[str]):
    cursor = _FakeCursor(rows, cols)
    db = MagicMock()

    @contextmanager
    def _connect():
        yield _FakeConn(cursor)

    db.connect = _connect
    db._cursor = cursor
    return db


# ── /stats/series ────────────────────────────────────────────────


def test_series_requires_geo_or_nuts_level():
    os.environ["STATS_DATABASE_URL"] = "postgresql://u:p@h/d"
    try:
        r = _client().get("/stats/series?dataset=nama_10r_2gdp")
        assert r.status_code == 400
        assert "geo" in r.json()["detail"].lower()
    finally:
        del os.environ["STATS_DATABASE_URL"]


def test_series_503_when_url_missing():
    os.environ.pop("STATS_DATABASE_URL", None)
    r = _client().get("/stats/series?dataset=nama_10r_2gdp&geo=DE")
    assert r.status_code == 503


def test_series_geo_param_builds_geo_filter():
    os.environ["STATS_DATABASE_URL"] = "postgresql://u:p@h/d"
    db = _fake_db_factory(rows=[], cols=[
        "geo_code", "year", "time", "dimensions", "value", "flags",
    ])
    try:
        with patch("src.api.routers.stats.StatsDatabase", return_value=db):
            r = _client().get(
                "/stats/series?dataset=nama_10r_2gdp&geo=DE&geo=FR",
            )
            assert r.status_code == 200
            sql = db._cursor.last_sql
            assert "geo_code = ANY(%s)" in sql
            assert ["nama_10r_2gdp", ["DE", "FR"]] == db._cursor.last_params
    finally:
        del os.environ["STATS_DATABASE_URL"]


def test_series_nuts_level_filters_by_code_length():
    os.environ["STATS_DATABASE_URL"] = "postgresql://u:p@h/d"
    db = _fake_db_factory(rows=[], cols=[
        "geo_code", "year", "time", "dimensions", "value", "flags",
    ])
    try:
        with patch("src.api.routers.stats.StatsDatabase", return_value=db):
            r = _client().get(
                "/stats/series?dataset=nama_10r_2gdp&nuts_level=2",
            )
            assert r.status_code == 200
            sql = db._cursor.last_sql
            # NUTS-2 codes are 4 chars (2 country + 2 region)
            assert "char_length(geo_code) = %s" in sql
            assert 4 in db._cursor.last_params
            payload = r.json()
            assert payload["nuts_level"] == 2
    finally:
        del os.environ["STATS_DATABASE_URL"]


def test_series_year_range_filters():
    os.environ["STATS_DATABASE_URL"] = "postgresql://u:p@h/d"
    db = _fake_db_factory(rows=[], cols=[
        "geo_code", "year", "time", "dimensions", "value", "flags",
    ])
    try:
        with patch("src.api.routers.stats.StatsDatabase", return_value=db):
            r = _client().get(
                "/stats/series?dataset=nama_10r_2gdp&geo=DE"
                "&start=2010&end=2020",
            )
            assert r.status_code == 200
            assert "time >= make_date(%s, 1, 1)" in db._cursor.last_sql
            assert "time <= make_date(%s, 12, 31)" in db._cursor.last_sql
            assert 2010 in db._cursor.last_params
            assert 2020 in db._cursor.last_params
    finally:
        del os.environ["STATS_DATABASE_URL"]


def test_series_dimensions_filter_validates_json():
    os.environ["STATS_DATABASE_URL"] = "postgresql://u:p@h/d"
    db = _fake_db_factory(rows=[], cols=[
        "geo_code", "year", "time", "dimensions", "value", "flags",
    ])
    try:
        with patch("src.api.routers.stats.StatsDatabase", return_value=db):
            r = _client().get(
                "/stats/series?dataset=demo_r_pjangrp3&geo=DE"
                "&dimensions=not-json",
            )
            assert r.status_code == 400
            assert "dimensions" in r.json()["detail"].lower()
    finally:
        del os.environ["STATS_DATABASE_URL"]


def test_series_nuts_level_bounds_validated():
    os.environ["STATS_DATABASE_URL"] = "postgresql://u:p@h/d"
    try:
        # NUTS levels go 0..3 — anything else is a 422 from FastAPI
        r = _client().get("/stats/series?dataset=x&nuts_level=4")
        assert r.status_code == 422
    finally:
        del os.environ["STATS_DATABASE_URL"]
