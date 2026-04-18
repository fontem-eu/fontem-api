"""Unit tests for Persistence using a mocked psycopg2 connection.

Pure integration against a live Postgres is out of scope for this
repo's current test harness; this level is enough to catch SQL-shape
mistakes and verify the dedup contract.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.reasoner.persistence import Persistence
from src.reasoner.rule import Finding


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/test")


@pytest.fixture
def mock_conn():
    with patch("src.reasoner.persistence.psycopg2") as mock_pg:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        mock_pg.connect.return_value = conn
        mock_pg.extras = MagicMock()
        yield {"pg": mock_pg, "conn": conn, "cur": cur}


def test_upsert_finding_sends_single_row_with_dedup_key(mock_conn):
    finding = Finding(
        rule_id="orphan-company",
        severity="warning",
        confidence=1.0,
        target_ids=["c1", "c2"],
        message="orphan",
        payload={"country": "DE"},
    )
    Persistence().upsert_finding(finding)

    execute_values = mock_conn["pg"].extras.execute_values
    execute_values.assert_called_once()
    args, _kwargs = execute_values.call_args
    # args: (cursor, sql, rows)
    assert args[0] is mock_conn["cur"]
    sql = args[1]
    rows = list(args[2])
    assert "ON CONFLICT (rule_id, finding_key) DO UPDATE" in sql
    assert "last_seen_at = now()" in sql
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "orphan-company"        # rule_id
    assert row[1] == finding.finding_key()   # finding_key
    assert row[2] == "warning"               # severity
    assert row[3] == 1.0                     # confidence
    assert json.loads(row[4]) == ["c1", "c2"]  # sorted target_ids
    assert row[5] == "orphan"                # message
    assert json.loads(row[6]) == {"country": "DE"}  # payload


def test_upsert_many_batches_all_rows(mock_conn):
    findings = [
        Finding(rule_id="r", severity="info", confidence=0.5,
                target_ids=[f"t{i}"], message="", payload={})
        for i in range(3)
    ]
    n = Persistence().upsert_many(findings)
    assert n == 3
    _, _, rows = mock_conn["pg"].extras.execute_values.call_args[0]
    assert len(list(rows)) == 3


def test_upsert_many_empty_is_noop(mock_conn):
    n = Persistence().upsert_many([])
    assert n == 0
    mock_conn["pg"].extras.execute_values.assert_not_called()


def test_record_audit_writes_one_row(mock_conn):
    Persistence().record_audit(
        rule_id="orphan-company",
        finding_key="abc123",
        run_id="run-42",
        action="auto-applied",
        summary="merged dup",
        payload={"a": 1},
    )
    mock_conn["cur"].execute.assert_called_once()
    sql, params = mock_conn["cur"].execute.call_args[0]
    assert "INSERT INTO reasoner_audit" in sql
    assert params[0] == "orphan-company"
    assert params[1] == "abc123"
    assert params[2] == "run-42"
    assert params[3] == "auto-applied"
    assert json.loads(params[5]) == {"a": 1}


def test_mark_applied_updates_status(mock_conn):
    Persistence().mark_applied("rule-x", "key-y")
    sql, params = mock_conn["cur"].execute.call_args[0]
    assert "UPDATE reasoner_findings" in sql
    assert "SET status = 'applied'" in sql
    assert params == ("rule-x", "key-y")


def test_dsn_substitutes_postgres_password(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:$(POSTGRES_PASSWORD)@pg:5432/db",
    )
    monkeypatch.setenv("POSTGRES_PASSWORD", "sup3r-secret")
    # Directly call the dsn helper for focused coverage.
    from src.reasoner.persistence import _dsn
    got = _dsn()
    assert got.startswith("postgresql://")
    assert "sup3r-secret" in got
    assert "$(POSTGRES_PASSWORD)" not in got


def test_dsn_raises_when_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REASONER_DATABASE_URL", raising=False)
    from src.reasoner.persistence import _dsn
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        _dsn()
