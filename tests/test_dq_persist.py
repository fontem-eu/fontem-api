"""Result persistence: one run_at per batch, self-bootstrapping DDL."""
from unittest import mock

from src.data_quality.assertions.persist import persist_results
from src.data_quality.assertions.runner import AssertionResult


def test_persist_writes_one_row_per_result(monkeypatch):
    executed = []

    class _Cur:
        def execute(self, q, *_a):
            executed.append(("execute", q))

        def executemany(self, q, rows):
            executed.append(("many", q, rows))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            executed.append(("commit",))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("src.data_quality.assertions.persist.psycopg",
                        mock.MagicMock(connect=lambda *a, **k: _Conn()))
    results = [
        AssertionResult("keys.x", "keys", "T", "block", "pass", "0"),
        AssertionResult("refs.y", "refs", "U", "block", "fail", "3 bad"),
    ]
    n = persist_results("postgresql://x", results)
    assert n == 2
    ddl = [e for e in executed if e[0] == "execute"][0][1]
    assert "CREATE TABLE IF NOT EXISTS events.dq_result" in ddl
    many = [e for e in executed if e[0] == "many"][0]
    rows = many[2]
    assert len(rows) == 2
    # single run_at shared across the batch
    assert rows[0][0] == rows[1][0]
    assert rows[1][1:] == ("refs.y", "refs", "block", "fail", "3 bad")
    assert ("commit",) in executed
