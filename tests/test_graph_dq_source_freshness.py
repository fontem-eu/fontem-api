"""Tests for GraphDataQualitySource.get_source_freshness.

Like the connectedness tests, these guard the Python glue around the
:DataSource markers — age computation, stale flagging, neo4j-DateTime
unwrapping. The Cypher itself is a single MATCH; we don't bother
exercising it through a real Neo4j here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.data.graph.graph_data_quality import GraphDataQualitySource


class _FakeNeoDateTime:  # pylint: disable=too-few-public-methods
    """Minimal stand-in for neo4j.time.DateTime — just enough for
    `to_native()` to return the underlying python datetime."""

    def __init__(self, dt: datetime) -> None:
        self._dt = dt

    def to_native(self) -> datetime:
        """Mirror the neo4j.time.DateTime API."""
        return self._dt


def _client_with_data_sources(rows: list[dict]) -> MagicMock:
    """Build a fake Neo4jClient whose session.run().data() returns the
    supplied rows."""
    session = MagicMock()
    result = MagicMock()
    result.data.return_value = rows
    session.run.return_value = result
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.session.return_value = session
    return client


def test_source_freshness_empty_graph_returns_empty_sources():
    """No :DataSource nodes → empty list, generated_at populated.
    Lets the dashboard render an honest 'no data yet' state."""
    client = _client_with_data_sources([])
    source = GraphDataQualitySource(client)
    result = source.get_source_freshness()
    assert not result["sources"]
    assert result["generated_at"] is not None


def test_source_freshness_marks_old_loads_stale():
    """A row whose age exceeds expected_cadence_hours is flagged
    stale; one within the cadence is not. Both numbers come back
    rounded to 2 decimals."""
    now = datetime.now(timezone.utc)
    fresh_dt = now - timedelta(hours=2)
    stale_dt = now - timedelta(hours=400)
    rows = [
        {
            "id": "sanctions",
            "label": "EU consolidated sanctions",
            "coverage_start": "2026-01-01",
            "coverage_end": "2026-04-29",
            "last_loaded": _FakeNeoDateTime(fresh_dt),
            "record_count": 3015,
            "expected_cadence_hours": 25,
        },
        {
            "id": "openfigi",
            "label": "OpenFIGI ticker enrichment",
            "coverage_start": None,
            "coverage_end": None,
            "last_loaded": _FakeNeoDateTime(stale_dt),
            "record_count": 12345,
            "expected_cadence_hours": 200,
        },
    ]
    client = _client_with_data_sources(rows)
    source = GraphDataQualitySource(client)
    result = source.get_source_freshness()
    by_id = {s["id"]: s for s in result["sources"]}
    assert by_id["sanctions"]["stale"] is False
    assert by_id["sanctions"]["age_hours"] == 2.0
    assert by_id["openfigi"]["stale"] is True
    assert by_id["openfigi"]["age_hours"] == 400.0


def test_source_freshness_handles_missing_last_loaded():
    """Defensive: if last_loaded is null (shouldn't happen in
    practice but the property is technically optional), age stays
    None and stale stays False."""
    rows = [
        {
            "id": "lobbying",
            "label": "EU Transparency Register",
            "coverage_start": "2024-01-01",
            "coverage_end": "2026-04-29",
            "last_loaded": None,
            "record_count": 14000,
            "expected_cadence_hours": 200,
        },
    ]
    client = _client_with_data_sources(rows)
    source = GraphDataQualitySource(client)
    result = source.get_source_freshness()
    assert result["sources"][0]["age_hours"] is None
    assert result["sources"][0]["stale"] is False
    assert result["sources"][0]["last_loaded"] is None


def test_source_freshness_handles_naive_datetime():
    """If for some reason the stored datetime is naive (no tz), we
    treat it as UTC rather than crashing on the subtraction."""
    naive = datetime.utcnow() - timedelta(hours=5)  # noqa: DTZ003 — deliberate
    rows = [
        {
            "id": "contracts",
            "label": "TED public procurement contracts",
            "coverage_start": "2024-01-01",
            "coverage_end": "2026-04-29",
            "last_loaded": _FakeNeoDateTime(naive),
            "record_count": 100000,
            "expected_cadence_hours": 24 * 35,
        },
    ]
    client = _client_with_data_sources(rows)
    source = GraphDataQualitySource(client)
    result = source.get_source_freshness()
    assert result["sources"][0]["age_hours"] is not None
    assert result["sources"][0]["age_hours"] >= 4.9
