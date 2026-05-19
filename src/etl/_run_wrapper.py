"""Wrapper that records every ETL CronJob invocation to events.etl_run.

Each cronjob template renders this as the container command instead of
calling the loader module directly:

    python -m src.etl._run_wrapper <module>

The wrapper imports the loader module, calls its `main()` inside a
`fontem_events.RunLog` context, and propagates the exit code. The
RunLog records `status='running'` on entry, then `success` or `failed`
on exit; SIGKILL / OOM / activeDeadlineSeconds leave the row at
`running` so the dashboard can flag it as crashed.

The cronjob name + image tag are read from env vars (CRONJOB_NAME +
IMAGE_TAG) wired by the shared chart template.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys

from fontem_events import RunLog

logger = logging.getLogger(__name__)


def _resolve_cronjob_name() -> str:
    """CRONJOB_NAME env wins; fall back to argv[0]-derived for ad-hoc use."""
    name = os.environ.get("CRONJOB_NAME")
    if name:
        return name
    # When operators run `python -m src.etl._run_wrapper src.etl.load_X`
    # locally without setting CRONJOB_NAME, derive a best-effort label
    # so the row isn't empty. Production cronjobs always set the env.
    if len(sys.argv) >= 2:
        return "manual-" + sys.argv[1].rsplit(".", 1)[-1]
    return "manual-unknown"


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        sys.stderr.write(
            "usage: python -m src.etl._run_wrapper <loader_module> [args...]\n"
        )
        return 2

    module_name, loader_argv = argv[0], argv[1:]

    cronjob_name = _resolve_cronjob_name()
    # Skip RunLog entirely if EVENTS_DATABASE_URL isn't set (local dev
    # without the events DB). The loader still runs unchanged — we'd
    # rather lose the row than fail the actual ETL on dev boxes.
    if not os.environ.get("EVENTS_DATABASE_URL"):
        logger.warning(
            "%s: EVENTS_DATABASE_URL not set; running without run-log",
            cronjob_name,
        )
        module = importlib.import_module(module_name)
        return _invoke(module, loader_argv)

    try:
        with RunLog.from_env(cronjob_name=cronjob_name) as run:
            module = importlib.import_module(module_name)
            rc = _invoke(module, loader_argv)
            # The loader's stdout summary is captured by the cluster
            # log stream; we copy the convention from the Kuma trap
            # and pull a "Done: ..." line if the loader's main()
            # stashed one on the module. Loaders that don't bother
            # leave the summary NULL — fine.
            summary = getattr(module, "LAST_SUMMARY", None)
            if isinstance(summary, str):
                run.set_summary(summary)
            if rc != 0:
                # Non-zero RC from a loader that didn't raise: synthesise
                # a failure so the row records the bad outcome.
                raise SystemExit(rc)
        return 0
    except SystemExit as exc:
        # SystemExit with code != 0 was already promoted to status=failed
        # by RunLog (the exception propagated through __exit__). Re-raise
        # as the process exit code so the cronjob sees the failure.
        return int(exc.code) if isinstance(exc.code, int) else 1


def _invoke(module, loader_argv: list[str]) -> int:
    if not hasattr(module, "main"):
        sys.stderr.write(
            f"loader {module.__name__} has no main() — cannot wrap with RunLog\n"
        )
        return 2
    result = module.main(loader_argv)
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
