"""Tests for GET /data-quality/pipeline — per-source pipeline health."""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import psycopg
from fastapi.testclient import TestClient

from src.api.dq_sources import DATA_SOURCES
from src.atlas_api.sources.etl_runs import EtlRunsSource


def _client(events_dsn: str | None = "postgresql://test:test@h/events"):
    env: dict[str, str] = {"STATS_DATABASE_URL": "postgresql://t:t@h/s"}
    if events_dsn is not None:
        env["EVENTS_DATABASE_URL"] = events_dsn
    with patch.dict("os.environ", env, clear=False):
        if events_dsn is None:
            os.environ.pop("EVENTS_DATABASE_URL", None)
        import importlib  # pylint: disable=import-outside-toplevel
        from src.api import app as app_module  # pylint: disable=import-outside-toplevel
        importlib.reload(app_module)
        return TestClient(app_module.app)


def test_pipeline_503_when_unconfigured():
    assert _client(events_dsn=None).get("/data-quality/pipeline").status_code == 503


def test_pipeline_joins_registry_with_metrics_and_derives_fields():
    fresh = datetime.now(timezone.utc) - timedelta(hours=2)
    metrics = {
        "by_producer": {
            "load_gleif": {
                "events_total": 1000, "events_30d": 200,
                "last_event_at": fresh, "deadletter": 35,
            },
        },
        "by_cronjob": {
            "etl-gleif": {
                "last_run_at": fresh, "last_run_finished_at": fresh,
                "last_run_status": "success", "last_run_summary": "ok",
            },
        },
    }
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.pipeline_metrics",
        return_value=metrics,
    ):
        r = _client().get("/data-quality/pipeline")
    assert r.status_code == 200
    body = r.json()
    # Every registered source appears, even with no metrics.
    assert {s["id"] for s in body} == {s.id for s in DATA_SOURCES}
    gleif = next(s for s in body if s["id"] == "gleif")
    assert gleif["events_total"] == 1000
    assert gleif["last_run_status"] == "success"
    assert gleif["deadletter"] == 35
    assert gleif["deadletter_pct"] == 3.5           # 35/1000
    assert gleif["stale"] is False                  # 2h < 48h SLA
    assert gleif["age_hours"] is not None and gleif["age_hours"] < 48


def test_pipeline_flags_stale_and_missing_sources():
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    metrics = {
        "by_producer": {
            "load_firds": {
                "events_total": 500, "events_30d": 0,
                "last_event_at": old, "deadletter": 0,
            },
        },
        "by_cronjob": {
            "etl-firds": {
                "last_run_at": old, "last_run_finished_at": old,
                "last_run_status": "failed", "last_run_summary": None,
            },
        },
    }
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.pipeline_metrics",
        return_value=metrics,
    ):
        body = _client().get("/data-quality/pipeline").json()
    firds = next(s for s in body if s["id"] == "firds")
    assert firds["stale"] is True                   # 2020 ≫ 48h
    assert firds["last_run_status"] == "failed"
    # A source with no metrics at all is stale with zero volume.
    nuts = next(s for s in body if s["id"] == "nuts")
    assert nuts["events_total"] == 0
    assert nuts["stale"] is True
    assert nuts["deadletter_pct"] == 0.0


def test_registry_producers_and_cronjobs_are_unique():
    producers = [s.producer for s in DATA_SOURCES]
    assert len(producers) == len(set(producers)), "duplicate producer in registry"
    assert all(s.id and s.label and s.theme for s in DATA_SOURCES)


def test_pipeline_metrics_reads_events_db():
    """Exercise the SQL reader itself (the endpoint tests mock it): a
    mocked psycopg connection feeds the three aggregate queries and we
    assert the producer/cronjob shaping."""
    when = datetime(2026, 6, 1, tzinfo=timezone.utc)
    cur = MagicMock()
    cur.fetchall.side_effect = [
        [("load_gleif", 1000, 200, when)],             # entity_events agg
        [("load_gleif", 35)],                           # dead_letter join
        [("etl-gleif", when, when, "success", "ok")],   # latest run per cronjob
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    @contextmanager
    def fake_connect(_self):
        yield conn

    src = EtlRunsSource("postgresql://t:t@h/events")
    with patch.object(EtlRunsSource, "_connect", fake_connect):
        m = src.pipeline_metrics()

    assert m["by_producer"]["load_gleif"] == {
        "events_total": 1000, "events_30d": 200,
        "last_event_at": when, "deadletter": 35,
    }
    assert m["by_cronjob"]["etl-gleif"]["last_run_status"] == "success"
    assert m["by_cronjob"]["etl-gleif"]["last_run_summary"] == "ok"


def test_pipeline_metrics_empty_when_tables_missing():
    cur = MagicMock()
    cur.execute.side_effect = psycopg.errors.UndefinedTable("no events table")
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    @contextmanager
    def fake_connect(_self):
        yield conn

    src = EtlRunsSource("postgresql://t:t@h/events")
    with patch.object(EtlRunsSource, "_connect", fake_connect):
        m = src.pipeline_metrics()
    assert m == {"by_producer": {}, "by_cronjob": {}}


def test_timeline_returns_per_day_events_for_known_source():
    from datetime import date  # pylint: disable=import-outside-toplevel
    rows = [{"day": date(2026, 6, 1), "events": 10},
            {"day": date(2026, 6, 2), "events": 7}]
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.events_timeline",
        return_value=rows,
    ) as m:
        r = _client().get("/data-quality/pipeline/gleif/timeline?days=30")
    assert r.status_code == 200
    assert r.json() == [
        {"day": "2026-06-01", "events": 10},
        {"day": "2026-06-02", "events": 7},
    ]
    # producer (not the slug) is what reaches the reader.
    assert m.call_args.args[0] == "load_gleif"


def test_timeline_404_for_unknown_source():
    assert _client().get("/data-quality/pipeline/nope/timeline").status_code == 404


def test_timeline_503_when_unconfigured():
    r = _client(events_dsn=None).get("/data-quality/pipeline/gleif/timeline")
    assert r.status_code == 503


def test_events_timeline_reader_shapes_rows():
    from datetime import date  # pylint: disable=import-outside-toplevel
    cur = MagicMock()
    cur.fetchall.return_value = [(date(2026, 6, 1), 5), (date(2026, 6, 2), 9)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    @contextmanager
    def fake_connect(_self):
        yield conn

    src = EtlRunsSource("postgresql://t:t@h/events")
    with patch.object(EtlRunsSource, "_connect", fake_connect):
        out = src.events_timeline("load_gleif", days=30)
    assert out == [{"day": date(2026, 6, 1), "events": 5},
                   {"day": date(2026, 6, 2), "events": 9}]
