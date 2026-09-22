"""What the fontem-stats sync CronJobs run.

    python -m src.stats_etl.cron daily  --datasets-file /etc/fontem-stats/datasets.txt
    python -m src.stats_etl.cron weekly --datasets-file … --log-dir /run-logs

This used to be a shell script inside the CronJob template. The ETL image
moved to a Chainguard base on 2026-09-01, which ships no /bin/sh and no
curl, so every run died before it started (`StartError: exec "/bin/sh"`)
and the Eurostat catalogue stopped refreshing (gitops #543). The steps are
the same; they are just Python now, so they run on any image the CLI runs
on and can be tested.

daily   register-seed, nuts-polygons (a failure is a warning — the sync
        still runs), then sync whatever went stale in the last day.
weekly  register-seed, then a forced sync of every enabled dataset.

Uptime Kuma gets one push at the very end: `up` only when the sync
exited 0, `down` for everything else — including a SIGTERM from
activeDeadlineSeconds, which is how these jobs have usually died. The
message is the CLI's own summary ("3 synced, 1 skipped, 0 failed").
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import signal
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from . import cli

class _Tee(io.TextIOBase):
    """Writes through to the real stream as it happens (so `kubectl
    logs` is live and a run killed at its deadline has still said what
    it was doing), keeps the text for the Kuma summary, and mirrors it
    to a file when one could be opened."""

    def __init__(self, stream, mirror=None):
        super().__init__()
        self._stream, self._mirror = stream, mirror
        self.lines: list[str] = []

    def write(self, text: str) -> int:
        self._stream.write(text)
        self._stream.flush()
        if self._mirror is not None:
            self._mirror.write(text)
            self._mirror.flush()
        self.lines.append(text)
        return len(text)

    def flush(self) -> None:
        self._stream.flush()


def _open_log(log_dir: str | None, cadence: str):
    """The weekly mirrors its output to a node directory that outlives the
    pod. That is a convenience: a directory we cannot write must not stop
    the sync."""
    if not log_dir:
        return None
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path(log_dir) / f"{cadence}-{stamp}.log"
    try:
        return path.open("w", encoding="utf-8")
    except OSError as exc:
        print(f"::warning:: no run log at {path} ({exc}); continuing",
              file=sys.stderr)
        return None


def _summary(text: str) -> str:
    """The CLI's last "summary: 3 synced, 1 skipped, 0 failed, 900 rows"
    line as Kuma shows it: "3+synced+1+skipped+0+failed"."""
    for line in reversed(text.splitlines()):
        if not line.startswith("summary:"):
            continue
        parts = [p.strip() for p in line[len("summary:"):].split(",")]
        kept = [p for p in parts if p.split(" ")[-1] in ("synced", "skipped", "failed")]
        if kept:
            return "+".join(kept).replace(" ", "+")
    return "unknown"


def push_kuma(url: str | None, status: str, summary: str) -> None:
    """Best effort. Monitoring must never fail the job it monitors."""
    if not url:
        return
    query = urllib.parse.urlencode({"status": status, "msg": summary, "ping": ""})
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=10):  # noqa: S310
            pass
    except OSError as exc:
        print(f"::warning:: kuma push failed: {exc}", file=sys.stderr)


def _steps(cadence: str, datasets_file: str) -> list[tuple[list[str], bool]]:
    """(cli argv, must succeed) in order; the sync is always last."""
    seed = (["register-seed", "--from-file", datasets_file], True)
    if cadence == "daily":
        return [seed,
                (["nuts-polygons", "--version", "2024"], False),
                (["sync", "--stale-after", "1d"], True)]
    return [seed, (["sync", "--all", "--force"], True)]


def run(cadence: str, datasets_file: str) -> int:
    for argv, required in _steps(cadence, datasets_file):
        print(f"[{cadence}] {' '.join(argv)}")
        try:
            rc = cli.main(argv)
        except Exception as exc:  # pylint: disable=broad-except
            print(f"{argv[0]} raised {type(exc).__name__}: {exc}", file=sys.stderr)
            rc = 1
        if rc and required:
            return rc
        if rc:
            print(f"::warning:: {argv[0]} failed (rc={rc}); continuing — "
                  "the sync still runs")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stats_etl.cron", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cadence", choices=("daily", "weekly"))
    parser.add_argument("--datasets-file", default="/etc/fontem-stats/datasets.txt")
    parser.add_argument("--log-dir", default=None)
    args = parser.parse_args(argv)

    mirror = _open_log(args.log_dir, args.cadence)
    out, err = _Tee(sys.stdout, mirror), _Tee(sys.stderr, mirror)
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err

    def _terminated(signum, _frame):
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, _terminated)
    rc = 1
    try:
        print(f"run start {dt.datetime.now(dt.UTC):%FT%TZ}")
        rc = run(args.cadence, args.datasets_file)
        return rc
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
        raise
    finally:
        print(f"run end {dt.datetime.now(dt.UTC):%FT%TZ} rc={rc}")
        sys.stdout, sys.stderr = real_out, real_err
        signal.signal(signal.SIGTERM, previous)
        push_kuma(os.environ.get("KUMA_PUSH_URL"), "up" if rc == 0 else "down",
                  _summary("".join(out.lines)))
        if mirror is not None:
            mirror.close()


if __name__ == "__main__":
    sys.exit(main())
