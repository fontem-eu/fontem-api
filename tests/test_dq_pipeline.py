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


# ── /data-quality/consumer-lag ────────────────────────────────────────────
# Per-source freshness cannot show this: a source can be ingesting
# perfectly while the consumer writing it into Neo4j has stalled. The lag
# is a queue depth, so a consumer that is merely slow and one that has
# stopped both show a rising number — updated_at is what separates them.

def _lag(**overrides):
    base = {
        "consumer_name": "neo4j_sink",
        "last_seq": 65_711_957,
        "head_seq": 65_711_957,
        "lag": 0,
        "updated_at": datetime(2026, 8, 31, 5, 17, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def test_consumer_lag_reports_each_consumer():
    rows = [
        _lag(),
        _lag(consumer_name="consolidator_trigger",
             last_seq=50_366_134, lag=15_345_823),
    ]
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.consumer_lag",
        return_value=rows,
    ):
        r = _client().get("/data-quality/consumer-lag")
    assert r.status_code == 200
    body = r.json()
    assert {b["consumer_name"] for b in body} == {
        "neo4j_sink", "consolidator_trigger"}
    trigger = next(b for b in body if b["consumer_name"] == "consolidator_trigger")
    assert trigger["lag"] == 15_345_823
    assert trigger["head_seq"] == 65_711_957


def test_consumer_lag_keeps_a_caught_up_consumer_at_zero():
    """Zero is a real value here, not 'no data' — the panel must be able
    to show green rather than blank."""
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.consumer_lag",
        return_value=[_lag()],
    ):
        r = _client().get("/data-quality/consumer-lag")
    assert r.json()[0]["lag"] == 0


def test_consumer_lag_503_when_unconfigured():
    r = _client(events_dsn=None).get("/data-quality/consumer-lag")
    assert r.status_code == 503


def test_consumer_lag_empty_before_bootstrap():
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.consumer_lag",
        return_value=[],
    ):
        r = _client().get("/data-quality/consumer-lag")
    assert r.status_code == 200
    assert r.json() == []


# ── the SQL readers behind the two new endpoints ──────────────────────────
# The endpoint tests above mock these methods out, so without this the SQL
# and the arithmetic inside it are never executed.

def _src_with(cur):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    @contextmanager
    def fake_connect(_self):
        yield conn

    return EtlRunsSource("postgresql://t:t@h/events"), fake_connect


def test_consumer_lag_computes_distance_from_the_head():
    """One head read for all consumers, then lag = head - offset."""
    when = datetime(2026, 8, 31, 5, 17, tzinfo=timezone.utc)
    cur = MagicMock()
    cur.fetchone.return_value = (65_711_957,)
    cur.fetchall.return_value = [
        ("consolidator_trigger", 50_366_134, when),
        ("neo4j_sink", 65_711_957, when),
    ]
    src, fake_connect = _src_with(cur)
    with patch.object(EtlRunsSource, "_connect", fake_connect):
        rows = src.consumer_lag()

    by_name = {r["consumer_name"]: r for r in rows}
    assert by_name["consolidator_trigger"]["lag"] == 15_345_823
    assert by_name["consolidator_trigger"]["head_seq"] == 65_711_957
    assert by_name["neo4j_sink"]["lag"] == 0


def test_consumer_lag_never_reports_a_negative():
    """An offset past the head (a replayed or rewound consumer) would
    otherwise render as a negative queue depth, which reads as nonsense
    on the dashboard."""
    cur = MagicMock()
    cur.fetchone.return_value = (100,)
    cur.fetchall.return_value = [("odd_consumer", 150, None)]
    src, fake_connect = _src_with(cur)
    with patch.object(EtlRunsSource, "_connect", fake_connect):
        rows = src.consumer_lag()
    assert rows[0]["lag"] == 0


def test_consumer_lag_handles_an_empty_event_log():
    """coalesce(max(seq), 0) — a fresh cluster has no events at all."""
    cur = MagicMock()
    cur.fetchone.return_value = (0,)
    cur.fetchall.return_value = [("neo4j_sink", 0, None)]
    src, fake_connect = _src_with(cur)
    with patch.object(EtlRunsSource, "_connect", fake_connect):
        rows = src.consumer_lag()
    assert rows[0]["lag"] == 0
    assert rows[0]["head_seq"] == 0


def test_consumer_lag_empty_when_the_table_is_missing():
    cur = MagicMock()
    cur.execute.side_effect = psycopg.errors.UndefinedTable("no offsets table")
    src, fake_connect = _src_with(cur)
    with patch.object(EtlRunsSource, "_connect", fake_connect):
        rows = src.consumer_lag()
    assert isinstance(rows, list) and not rows


def test_recent_runs_by_cronjob_passes_the_per_job_bound():
    when = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)
    cur = MagicMock()
    cur.description = [
        MagicMock(name=n) for n in range(8)
    ]
    for col, nm in zip(cur.description, [
        "run_id", "cronjob_name", "image_tag", "started_at",
        "finished_at", "status", "summary", "error_message",
    ]):
        col.name = nm
    cur.fetchall.return_value = [
        (1, "etl-gleif", "v1", when, when, "success", "ok", None),
    ]
    src, fake_connect = _src_with(cur)
    with patch.object(EtlRunsSource, "_connect", fake_connect):
        rows = src.recent_runs_by_cronjob(per_job=4)

    assert rows[0]["cronjob_name"] == "etl-gleif"
    assert rows[0]["status"] == "success"
    # The bound reaches the query rather than being applied afterwards.
    assert cur.execute.call_args.args[1] == (4,)


def test_recent_runs_by_cronjob_empty_when_the_table_is_missing():
    cur = MagicMock()
    cur.execute.side_effect = psycopg.errors.UndefinedTable("no etl_run table")
    src, fake_connect = _src_with(cur)
    with patch.object(EtlRunsSource, "_connect", fake_connect):
        rows = src.recent_runs_by_cronjob()
    assert isinstance(rows, list) and not rows


def test_events_guard_raises_503_when_unconfigured():
    """The shared guard itself — every data-quality endpoint that reads the
    events store depends on it answering 503 rather than 500 on a cluster
    running without EVENTS_DATABASE_URL."""
    from fastapi import HTTPException  # pylint: disable=import-outside-toplevel
    from src.api.helpers import events_source_or_503  # pylint: disable=import-outside-toplevel

    request = MagicMock()
    request.app.state.etl_runs_source.configured = False
    try:
        events_source_or_503(request)
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "EVENTS_DATABASE_URL" in exc.detail
    else:
        raise AssertionError("expected a 503")


def test_events_guard_returns_the_source_when_configured():
    from src.api.helpers import events_source_or_503  # pylint: disable=import-outside-toplevel

    request = MagicMock()
    request.app.state.etl_runs_source.configured = True
    assert events_source_or_503(request) is request.app.state.etl_runs_source
