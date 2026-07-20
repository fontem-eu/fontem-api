"""Tests for the hybrid search router at GET /api/search/results.

The endpoint does one pgvector + tsvector query, RRF-fuses the two
ranks, and shapes each row for the SearchView Vue component (title
from embed_text[0], subtitle from [1], context from [2:]). These
tests exercise the shaping and fallback behaviour; the SQL itself is
covered by integration tests that run against a real Postgres.
"""
# pylint: disable=redefined-outer-name,unused-argument
from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest
from fastapi.testclient import TestClient

from src.api.app import app


class _FakeCursor:
    def __init__(self, rows, description):
        self._rows = rows
        self.description = description

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, *a, **k):
        return None

    def fetchall(self):
        return self._rows


class _FakeConn:
    read_only = False

    def __init__(self, rows, cols):
        self._rows = rows
        self._cols = cols

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def cursor(self):
        cols = [MagicMock(name=c) for c in self._cols]
        for m, name in zip(cols, self._cols):
            m.name = name
        return _FakeCursor(self._rows, cols)


@pytest.fixture
def app_and_client(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SEARCH_DATABASE_URL",
        "postgresql://x:x@localhost:5432/x",
    )
    # No linguistics → the endpoint falls back to lexical-only
    monkeypatch.delenv("LINGUISTICS_URL", raising=False)
    # module-level FastAPI singleton; env is read per-request
    client = TestClient(app)
    return app, client


def _stub_conn(monkeypatch, rows, cols):
    def _fake_connect(*a, **k):
        return _FakeConn(rows, cols)
    monkeypatch.setattr(psycopg, "connect", _fake_connect)


def test_empty_query_rejected(app_and_client):
    _, client = app_and_client
    r = client.get("/search/results?q=")
    assert r.status_code == 422  # fastapi min_length=1


def test_503_when_search_dsn_unset(monkeypatch):
    monkeypatch.delenv("SEARCH_DATABASE_URL", raising=False)
    monkeypatch.delenv("EVENTS_DATABASE_URL", raising=False)
    # module-level FastAPI singleton; env is read per-request
    client = TestClient(app)
    r = client.get("/search/results?q=hello")
    assert r.status_code == 503
    assert "SEARCH_DATABASE_URL" in r.json()["detail"]


def test_response_shape_and_row_derivation(app_and_client, monkeypatch):
    """Rows are reshaped from `embed_text` into title/subtitle/context."""
    _, client = app_and_client
    cols = [
        "entity_type", "entity_id", "embed_text", "country", "event_date",
        "lex_rank", "vec_rank", "rrf_score",
    ]
    rows = [
        # (type, id, embed_text, country, date, lex_rank, vec_rank, rrf_score)
        ("company", "abc",
         "Siemens AG — SIE · Siemens — Munich — DE — AG",
         "DE", None, 1, 2, 0.033),
        ("contract", "n42",
         "Cleaning services for HQ — PT",
         "PT", "2026-05-01", None, 1, 0.016),
    ]
    _stub_conn(monkeypatch, rows, cols)

    r = client.get("/search/results?q=Siemens&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "Siemens"
    assert body["mode"] == "lexical_only"  # no linguistics in this fixture
    assert body["backend"] is None
    assert body["has_more"] is False
    assert body["counts"] == {"company": 1, "contract": 1}
    assert len(body["results"]) == 2

    r0 = body["results"][0]
    assert r0["type"] == "company"
    assert r0["id"] == "abc"
    assert r0["title"] == "Siemens AG"
    assert r0["subtitle"] == "SIE · Siemens"
    assert "Munich" in r0["context"] and "AG" in r0["context"]
    assert r0["country"] == "DE"
    assert r0["date"] is None
    assert r0["score"] == pytest.approx(0.033)
    assert r0["meta"]["lex_rank"] == 1 and r0["meta"]["vec_rank"] == 2

    r1 = body["results"][1]
    assert r1["title"] == "Cleaning services for HQ"
    # single-segment title after country slice → subtitle picked as second
    assert r1["subtitle"] == "PT"
    assert r1["context"] == ""
    assert r1["date"] == "2026-05-01"
    assert r1["meta"]["lex_rank"] is None and r1["meta"]["vec_rank"] == 1


def test_has_more_reflects_overfetch(app_and_client, monkeypatch):
    """When SQL returns limit+1 rows we set has_more=True and drop the +1."""
    _, client = app_and_client
    cols = [
        "entity_type", "entity_id", "embed_text", "country", "event_date",
        "lex_rank", "vec_rank", "rrf_score",
    ]
    # limit=2 → over-fetch=3, and we return 3 rows → has_more=True, results has 2
    rows = [
        ("company", f"id-{i}", f"Name {i}", None, None, None, i, 0.01)
        for i in range(1, 4)
    ]
    _stub_conn(monkeypatch, rows, cols)

    body = client.get("/search/results?q=x&limit=2").json()
    assert body["has_more"] is True
    assert len(body["results"]) == 2
    assert [r["id"] for r in body["results"]] == ["id-1", "id-2"]


def test_types_facet_filter_forwarded(app_and_client, monkeypatch):
    """The `types` query param is parsed into a comma-separated list and
    forwarded as an array to the SQL. Verifies wiring — result content
    is stubbed."""
    _, client = app_and_client
    captured: dict = {}

    class _Cur(_FakeCursor):
        # In-test stub: signature intentionally accepts *a/**k to match
        # psycopg cursor.execute's positional-or-kwarg params call.
        # pylint: disable=arguments-differ
        def execute(self, *a, **k):
            params = a[1] if len(a) > 1 else k.get("params")
            if params is not None and "types" in params:
                captured["types"] = params["types"]
    class _C(_FakeConn):
        def cursor(self):
            cols = [MagicMock(name=c) for c in self._cols]
            for m, name in zip(cols, self._cols):
                m.name = name
            return _Cur(self._rows, cols)

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _C(
        [], ["entity_type", "entity_id", "embed_text", "country",
             "event_date", "lex_rank", "vec_rank", "rrf_score"]))

    r = client.get("/search/results?q=x&types=company,authority")
    assert r.status_code == 200
    assert captured.get("types") == ["company", "authority"]


def test_nuts_and_sector_filters_forwarded(app_and_client, monkeypatch):
    """Advanced-search filters: nuts + sector both get forwarded as SQL
    bind params. Also validates the strict nuts pattern (uppercase
    ISO-3166-2 style, up to 8 chars)."""
    _, client = app_and_client
    captured: dict = {}

    class _Cur(_FakeCursor):
        # pylint: disable=arguments-differ
        def execute(self, *a, **k):
            params = a[1] if len(a) > 1 else k.get("params")
            if params is not None:
                for key in ("nuts", "sector"):
                    if key in params:
                        captured[key] = params[key]

    class _C(_FakeConn):
        def cursor(self):
            cols = [MagicMock(name=c) for c in self._cols]
            for m, name in zip(cols, self._cols):
                m.name = name
            return _Cur(self._rows, cols)

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _C(
        [], ["entity_type", "entity_id", "embed_text", "country",
             "event_date", "nuts", "sector", "meta",
             "lex_rank", "vec_rank", "rrf_score"]))

    r = client.get("/search/results?q=x&nuts=PT18&sector=90")
    assert r.status_code == 200, r.text
    assert captured.get("nuts") == "PT18"
    assert captured.get("sector") == "90"


def test_nuts_rejects_bad_format(app_and_client):
    """Reject anything that isn't the [A-Z]{2}[A-Z0-9]{0,6} shape —
    keeps garbage out of the LIKE prefix and avoids injection surface."""
    _, client = app_and_client
    for bad in ("pt18", "18PT", "P", "PT18!", "'; DROP TABLE"):
        r = client.get(f"/search/results?q=x&nuts={bad}")
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"


def test_meta_fields_surface_in_response(app_and_client, monkeypatch):
    """Rows carrying a jsonb `meta` get merged into the per-result
    `meta` envelope so SearchView can render per-type extras (ticker,
    value_eur, etc.)."""
    _, client = app_and_client
    cols = [
        "entity_type", "entity_id", "embed_text", "country", "event_date",
        "nuts", "sector", "meta",
        "lex_rank", "vec_rank", "rrf_score",
    ]
    rows = [
        ("contract", "TED-1",
         "Cleaning services HQ — PT", "PT", "2026-05-01",
         "PT18", "90",
         {"cpv": "90910000", "value_eur": 350000, "value_tier": "M"},
         1, 2, 0.033),
    ]
    _stub_conn(monkeypatch, rows, cols)
    r = client.get("/search/results?q=cleaning&nuts=PT18&sector=90")
    assert r.status_code == 200
    row = r.json()["results"][0]
    assert row["meta"]["nuts"] == "PT18"
    assert row["meta"]["sector"] == "90"
    assert row["meta"]["cpv"] == "90910000"
    assert row["meta"]["value_tier"] == "M"
    assert row["meta"]["value_eur"] == 350000
