"""Assertion monitor endpoint: failing-only, catalog-enriched,
failing-since from history (gitops#290 follow-on)."""
from datetime import datetime, timezone

from src.api.routers.dq_assertions import get_assertion_monitor

T1 = datetime(2026, 7, 12, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 12, 4, 0, tzinfo=timezone.utc)


class _Cursor:
    """Scripted cursor: answers by query shape."""

    def __init__(self, script):
        self.script = script
        self._last = None

    def execute(self, q, params=None):
        for needle, answer in self.script:
            if needle in q:
                self._last = answer(params) if callable(answer) else answer
                return
        raise AssertionError(q)

    def fetchone(self):
        return self._last[0]

    def fetchall(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cursor):
        self._c = cursor

    def cursor(self):
        return self._c


def test_monitor_failing_only_with_since():
    rows = [
        ("values.contract_value_nonneg", "values", "block", "fail", "2 negative"),
        ("keys.company_gmr_id_present", "keys", "block", "pass", "0 violations"),
        ("pipeline.stuck_runs", "pipeline", "warn", "warn", "101 stuck"),
    ]
    cur = _Cursor([
        ("MAX(run_at) FROM events.dq_result\nWHERE assertion_id", [(T1,)]),
        ("MIN(run_at)", [(T2,)]),
        ("MAX(run_at)", [(T2,)]),
        ("SELECT assertion_id", rows),
    ])
    out = get_assertion_monitor(_Conn(cur))
    assert out["run_at"] == T2.isoformat()
    assert out["summary"] == {"pass": 1, "warn": 1, "fail": 1, "error": 0}
    ids = [f["id"] for f in out["failing"]]
    assert ids == ["values.contract_value_nonneg", "pipeline.stuck_runs"]
    f = out["failing"][0]
    assert f["description"]            # catalog rationale present
    assert f["failing_since"] == T2.isoformat()


def test_monitor_empty_history():
    cur = _Cursor([("MAX(run_at)", [(None,)])])
    out = get_assertion_monitor(_Conn(cur))
    assert out == {"run_at": None, "summary": None, "failing": []}
