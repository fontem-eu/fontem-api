"""Tests for src.etl._freshness.update_source.

The helper writes a single :DataSource marker per loader run and is
called from the tail of every successful ETL pipeline. Its contract
is best-effort: the loader must never blow up because the marker
write failed (that's a monitoring side-effect, not a data step).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.etl import _freshness


def _fake_driver():
    """Build a fake driver with a fake session/transaction that records
    every Cypher call."""
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    driver = MagicMock()
    driver.session.return_value = session
    return driver, session


def test_update_source_writes_constraint_and_merge():
    """First call against a fresh graph must run the CREATE CONSTRAINT
    and the MERGE — in that order, so the unique-id invariant is
    enforced before the first node lands."""
    driver, session = _fake_driver()

    _freshness.update_source(
        driver,
        source_id="sanctions",
        label="EU consolidated sanctions",
        coverage_start="2026-01-01",
        coverage_end="2026-04-29",
        record_count=3015,
        expected_cadence_hours=25,
    )

    calls = session.run.call_args_list
    assert len(calls) == 2
    constraint_cypher = calls[0].args[0]
    merge_cypher = calls[1].args[0]
    assert "CREATE CONSTRAINT" in constraint_cypher
    assert "DataSource" in constraint_cypher
    assert "MERGE (s:DataSource" in merge_cypher
    # MERGE call carries all the parameters as kwargs.
    kwargs = calls[1].kwargs
    assert kwargs["id"] == "sanctions"
    assert kwargs["label"] == "EU consolidated sanctions"
    assert kwargs["coverage_start"] == "2026-01-01"
    assert kwargs["record_count"] == 3015
    assert kwargs["expected_cadence_hours"] == 25


def test_update_source_skips_when_id_or_label_missing():
    """Empty id/label is a programming error, not a data error — log
    and short-circuit rather than write a degenerate node."""
    driver, session = _fake_driver()
    _freshness.update_source(
        driver, source_id="", label="something",
        coverage_start=None, coverage_end=None,
        record_count=0, expected_cadence_hours=25,
    )
    assert session.run.call_count == 0


def test_update_source_swallows_neo4j_failures(caplog):
    """A Neo4j connection failure must not propagate — the data load
    that called us already succeeded; we shouldn't sink a successful
    ETL run because the freshness write tripped."""
    driver = MagicMock()
    driver.session.side_effect = RuntimeError("neo4j down")

    # Should not raise.
    _freshness.update_source(
        driver, source_id="lobbying", label="EU Transparency Register",
        coverage_start="2024-01-01", coverage_end="2026-04-29",
        record_count=14000, expected_cadence_hours=200,
    )

    # And it should have logged the failure for ops to find.
    assert any(
        "freshness: marker update failed" in r.message
        for r in caplog.records
    )


def test_update_source_coerces_record_count_and_cadence():
    """Defensive: callers pass `None` for record_count when the loader
    didn't compute one. Coerce to 0 / 25 rather than passing None to
    Neo4j (which would set the property to null)."""
    driver, session = _fake_driver()
    _freshness.update_source(
        driver, source_id="firds", label="ESMA FIRDS",
        coverage_start=None, coverage_end=None,
        record_count=None, expected_cadence_hours=None,
    )
    kwargs = session.run.call_args_list[1].kwargs
    assert kwargs["record_count"] == 0
    assert kwargs["expected_cadence_hours"] == 25
