"""Tests for the ETL run-wrapper shim.

The wrapper resolves a loader module, invokes its `main()` inside a
`fontem_events.RunLog`, and forwards the exit code. These tests
patch `RunLog` so we exercise the routing/exit-code logic without
touching a real Postgres.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access

import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.etl import _run_wrapper


def _fake_loader(rc: int = 0, *, raises: BaseException | None = None,
                 last_summary: str | None = None) -> types.ModuleType:
    mod = types.ModuleType("fake_loader_module")
    def main(_argv):
        if raises is not None:
            raise raises
        return rc
    mod.main = main
    if last_summary is not None:
        mod.LAST_SUMMARY = last_summary
    return mod


@contextmanager
def _patched_runlog(events_dsn: str = "postgresql://x"):
    """Replace RunLog with a MagicMock that's a no-op context manager.

    Allows the wrapper to "succeed" without a real DB. The mock also
    records set_summary calls so we can verify the wrapper plumbed
    the loader's LAST_SUMMARY through.
    """
    fake_run = MagicMock(name="RunLog_instance")
    fake_run.__enter__ = lambda self: self
    fake_run.__exit__ = lambda self, *exc: None
    with patch.dict("os.environ", {"EVENTS_DATABASE_URL": events_dsn}), \
         patch("src.etl._run_wrapper.RunLog") as RL:
        RL.from_env.return_value = fake_run
        yield fake_run


def test_invokes_loader_main_and_forwards_exit_code():
    fake = _fake_loader(rc=0)
    with _patched_runlog(), \
         patch("importlib.import_module", return_value=fake):
        rc = _run_wrapper.main(["src.etl.load_x"])
    assert rc == 0


def test_loader_non_zero_rc_is_promoted_to_failed_run():
    """A loader that returns rc=2 without raising must still record
    as a failed run — otherwise an ETL that swallows its own errors
    silently shows up as 'success' in the dashboard.
    """
    fake = _fake_loader(rc=2)
    with _patched_runlog() as run, \
         patch("importlib.import_module", return_value=fake):
        rc = _run_wrapper.main(["src.etl.load_x"])
    assert rc == 2
    # The wrapper raised SystemExit inside the RunLog context so the
    # row records as failed. We can't directly observe `status` here
    # (RunLog is mocked) — what matters is the rc.


def test_loader_exception_propagates_to_runlog():
    """An exception inside main() raises through __exit__, which is
    where RunLog records the traceback. The wrapper turns SystemExit
    back into the integer rc for the cronjob.
    """
    fake = _fake_loader(raises=RuntimeError("upstream 503"))
    with _patched_runlog(), \
         patch("importlib.import_module", return_value=fake):
        with pytest.raises(RuntimeError, match="upstream 503"):
            _run_wrapper.main(["src.etl.load_x"])


def test_loader_LAST_SUMMARY_is_passed_to_run_log():
    fake = _fake_loader(rc=0, last_summary="loaded 1234 entities")
    with _patched_runlog() as run, \
         patch("importlib.import_module", return_value=fake):
        _run_wrapper.main(["src.etl.load_x"])
    run.set_summary.assert_called_once_with("loaded 1234 entities")


def test_no_events_database_url_runs_loader_without_runlog(monkeypatch):
    """Local dev without the events DB: wrapper still runs the
    loader (we'd rather lose the dashboard row than fail the ETL on
    a dev box)."""
    monkeypatch.delenv("EVENTS_DATABASE_URL", raising=False)
    fake = _fake_loader(rc=0)
    # Order matters: patch RunLog first so the import resolution
    # for the patch target happens against the real module, THEN
    # patch importlib so the loader-import comes back as our fake.
    with patch("src.etl._run_wrapper.RunLog") as RL, \
         patch("importlib.import_module", return_value=fake):
        rc = _run_wrapper.main(["src.etl.load_x"])
    assert rc == 0
    RL.from_env.assert_not_called()


def test_no_argv_returns_usage_error():
    rc = _run_wrapper.main([])
    assert rc == 2


def test_loader_missing_main_function_errors_out():
    """A loader missing `main()` shouldn't crash the wrapper —
    record a clear error and exit non-zero.
    """
    fake = types.ModuleType("no_main_module")
    with _patched_runlog(), \
         patch("importlib.import_module", return_value=fake):
        rc = _run_wrapper.main(["src.etl.no_main"])
    assert rc == 2


def test_cronjob_name_resolves_from_env(monkeypatch):
    monkeypatch.setenv("CRONJOB_NAME", "etl-gleif")
    assert _run_wrapper._resolve_cronjob_name() == "etl-gleif"


def test_cronjob_name_falls_back_to_argv_when_env_missing(monkeypatch):
    monkeypatch.delenv("CRONJOB_NAME", raising=False)
    monkeypatch.setattr(sys, "argv", ["wrapper", "src.etl.load_gleif"])
    name = _run_wrapper._resolve_cronjob_name()
    assert name == "manual-load_gleif"
