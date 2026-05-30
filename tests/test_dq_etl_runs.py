"""Tests for /data-quality/etl-runs.

Mocks ``EtlRunsSource.recent_runs`` so the router exercises its contract
(filters, validation, 503 on unconfigured) without needing a live events
DB.

The endpoint used to live at ``/atlas/etl-runs`` inside ``src/atlas_api/``.
Moving it under ``/data-quality`` decoupled atlas-health from the events
DB connection — see the docstring on ``src/atlas_api/app._attach_state``
and the matching tests in ``tests/atlas_api/test_routers.py``.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring

import os
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient


def _client(
    *,
    stats_dsn: str | None = "postgresql://test:test@h/stats",
    events_dsn: str | None = "postgresql://test:test@h/events",
):
    """Build a TestClient against the main API app.

    The DQ etl-runs endpoint shares ``app.state.etl_runs_source`` with
    Atlas (atlas owns the events-DB connection wiring), so the same env
    vars STATS_DATABASE_URL + EVENTS_DATABASE_URL drive both feature
    surfaces. Until tests/conftest.py grows a proper fixture, build the
    app inline with the env in place.
    """
    env: dict[str, str] = {}
    if stats_dsn is not None:
        env["STATS_DATABASE_URL"] = stats_dsn
    if events_dsn is not None:
        env["EVENTS_DATABASE_URL"] = events_dsn
    with patch.dict("os.environ", env, clear=False):
        if stats_dsn is None:
            os.environ.pop("STATS_DATABASE_URL", None)
        if events_dsn is None:
            os.environ.pop("EVENTS_DATABASE_URL", None)
        # Re-import inside the patched env so the app picks up the DSNs.
        import importlib  # pylint: disable=import-outside-toplevel
        from src.api import app as app_module  # pylint: disable=import-outside-toplevel
        importlib.reload(app_module)
        return TestClient(app_module.app)


def _row(**overrides):
    base = {
        "run_id": 1,
        "cronjob_name": "etl-gleif",
        "image_tag": "vb66060f",
        "started_at": datetime(2026, 5, 18, 3, 0, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 5, 18, 3, 42, tzinfo=timezone.utc),
        "status": "success",
        "summary": "loaded 2.4M LEIs",
        "error_message": None,
    }
    base.update(overrides)
    return base


def test_etl_runs_503_when_unconfigured():
    r = _client(events_dsn=None).get("/data-quality/etl-runs")
    assert r.status_code == 503


def test_etl_runs_returns_recent():
    rows = [
        _row(run_id=2, cronjob_name="etl-firds", status="failed",
             error_message="ReadTimeout: ESMA Solr after 60s"),
        _row(run_id=1),
    ]
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.recent_runs",
        return_value=rows,
    ):
        r = _client().get("/data-quality/etl-runs")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["cronjob_name"] == "etl-firds"
    assert body[0]["status"] == "failed"
    assert "ReadTimeout" in body[0]["error_message"]


def test_etl_runs_passes_filters():
    """cronjob_name + status query params reach the source."""
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.recent_runs",
        return_value=[],
    ) as mock:
        _client().get(
            "/data-quality/etl-runs"
            "?cronjob_name=etl-gleif&status=failed&limit=10",
        )
    mock.assert_called_once()
    _, kwargs = mock.call_args
    assert kwargs["cronjob_name"] == "etl-gleif"
    assert kwargs["status"] == "failed"
    assert kwargs["limit"] == 10


def test_etl_runs_caps_limit_to_etl_runs_row_limit():
    """Client-supplied limit can't exceed the configured cap."""
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.recent_runs",
        return_value=[],
    ) as mock:
        # Default cap is 200; ask for 500 (the schema's upper bound).
        _client().get("/data-quality/etl-runs?limit=500")
    _, kwargs = mock.call_args
    assert kwargs["limit"] == 200


def test_etl_runs_returns_empty_when_table_missing():
    """Pre-bootstrap cluster: source returns [] from
    psycopg.errors.UndefinedTable. Endpoint must not 500.
    """
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.recent_runs",
        return_value=[],
    ):
        r = _client().get("/data-quality/etl-runs")
    assert r.status_code == 200
    assert r.json() == []


def test_etl_runs_handles_running_status_without_finished_at():
    """A row that's still running (or crashed pre-exit) has
    finished_at NULL. Pydantic must accept it."""
    with patch(
        "src.atlas_api.sources.etl_runs.EtlRunsSource.recent_runs",
        return_value=[_row(status="running", finished_at=None,
                           summary=None, error_message=None)],
    ):
        r = _client().get("/data-quality/etl-runs")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["finished_at"] is None
    assert body[0]["status"] == "running"
