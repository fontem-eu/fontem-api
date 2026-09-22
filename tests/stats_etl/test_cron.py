"""The stats sync CronJobs' entrypoint (src.stats_etl.cron).

It replaced a shell script the hardened image cannot run (gitops #543),
so what is pinned here is what that script promised: the steps and
their order, which failures stop the run, and what Uptime Kuma is told.
"""
# pylint: disable=redefined-outer-name,unused-argument
import signal
import urllib.parse

import pytest

from src.stats_etl import cron


@pytest.fixture
def harness(monkeypatch):
    """Records the CLI calls and the Kuma push; `rcs`/`prints` script the CLI."""
    state = {"calls": [], "pushed": [], "rcs": {}, "prints": {}, "raises": {}}

    def fake_cli(argv):
        state["calls"].append(list(argv))
        if argv[0] in state["raises"]:
            raise state["raises"][argv[0]]
        if argv[0] in state["prints"]:
            print(state["prints"][argv[0]])
        return state["rcs"].get(argv[0], 0)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(url, timeout=None):
        state["pushed"].append(url)
        return _Resp()

    monkeypatch.setattr(cron.cli, "main", fake_cli)
    monkeypatch.setattr(cron.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("KUMA_PUSH_URL", "https://kuma/api/push/abc")
    return state


def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query,
                                       keep_blank_values=True))


def test_daily_runs_seed_polygons_then_a_stale_sync_and_reports_up(harness):
    harness["prints"]["sync"] = "summary: 3 synced, 1 skipped, 0 failed, 900 rows"
    assert cron.main(["daily", "--datasets-file", "/d.txt"]) == 0
    assert harness["calls"] == [
        ["register-seed", "--from-file", "/d.txt"],
        ["nuts-polygons", "--version", "2024"],
        ["sync", "--stale-after", "1d"],
    ]
    q = _query(harness["pushed"][0])
    assert harness["pushed"][0].startswith("https://kuma/api/push/abc?")
    assert q == {"status": "up", "msg": "3+synced+1+skipped+0+failed", "ping": ""}


def test_weekly_forces_every_dataset_and_skips_the_polygons(harness):
    assert cron.main(["weekly"]) == 0
    assert harness["calls"] == [
        ["register-seed", "--from-file", "/etc/fontem-stats/datasets.txt"],
        ["sync", "--all", "--force"],
    ]


def test_a_polygon_failure_is_a_warning_and_the_sync_still_runs(harness, capsys):
    harness["rcs"]["nuts-polygons"] = 1
    assert cron.main(["daily"]) == 0
    assert harness["calls"][-1][0] == "sync"
    assert "nuts-polygons failed" in capsys.readouterr().out
    assert _query(harness["pushed"][0])["status"] == "up"


def test_a_failed_sync_is_the_exit_code_and_kuma_hears_down(harness):
    harness["rcs"]["sync"] = 2
    harness["prints"]["sync"] = "summary: 1 synced, 0 skipped, 2 failed, 10 rows"
    assert cron.main(["daily"]) == 2
    q = _query(harness["pushed"][0])
    assert q["status"] == "down" and q["msg"] == "1+synced+0+skipped+2+failed"


def test_a_failed_seed_stops_the_run_before_any_sync(harness):
    harness["rcs"]["register-seed"] = 1
    assert cron.main(["weekly"]) == 1
    assert [c[0] for c in harness["calls"]] == ["register-seed"]
    q = _query(harness["pushed"][0])
    assert q["status"] == "down" and q["msg"] == "unknown"


def test_a_crash_inside_a_step_counts_as_its_failure(harness, capsys):
    harness["raises"]["sync"] = RuntimeError("connection refused")
    assert cron.main(["daily"]) == 1
    assert "RuntimeError: connection refused" in capsys.readouterr().err
    assert _query(harness["pushed"][0])["status"] == "down"


def test_a_deadline_kill_still_reports_down(harness, monkeypatch):
    """activeDeadlineSeconds ends the pod with SIGTERM — how these jobs
    have usually died. Kuma must hear about it rather than go silent."""
    def killed(argv):
        if argv[0] == "sync":
            signal.raise_signal(signal.SIGTERM)
        return 0

    monkeypatch.setattr(cron.cli, "main", killed)
    with pytest.raises(SystemExit) as exit_info:
        cron.main(["weekly"])
    assert exit_info.value.code == 128 + signal.SIGTERM
    assert _query(harness["pushed"][0])["status"] == "down"


def test_no_kuma_url_means_no_push(harness, monkeypatch):
    monkeypatch.delenv("KUMA_PUSH_URL")
    assert cron.main(["daily"]) == 0
    assert harness["pushed"] == []


def test_a_kuma_outage_never_fails_the_job(harness, monkeypatch, capsys):
    def down(url, timeout=None):
        raise OSError("kuma unreachable")

    monkeypatch.setattr(cron.urllib.request, "urlopen", down)
    assert cron.main(["daily"]) == 0
    assert "kuma push failed" in capsys.readouterr().err


def test_the_weekly_mirrors_its_output_to_the_log_dir(harness, tmp_path):
    harness["prints"]["sync"] = "summary: 42 synced, 0 skipped, 0 failed, 1 rows"
    assert cron.main(["weekly", "--log-dir", str(tmp_path)]) == 0
    (log,) = list(tmp_path.glob("weekly-*.log"))
    text = log.read_text()
    assert "run start" in text and "42 synced" in text and "rc=0" in text


def test_an_unwritable_log_dir_is_a_warning_not_a_failure(harness, tmp_path, capsys):
    """The hostPath directory is root-owned and the image runs as 65532."""
    missing = tmp_path / "does" / "not" / "exist"
    assert cron.main(["weekly", "--log-dir", str(missing)]) == 0
    assert "no run log" in capsys.readouterr().err
    assert harness["calls"][-1] == ["sync", "--all", "--force"]
