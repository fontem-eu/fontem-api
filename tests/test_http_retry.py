"""Tests for the ETL HTTP retry helper."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.etl._http_retry import (
    RateLimiter, call_with_retry, get_with_retry,
)


def _resp(status: int) -> httpx.Response:
    return httpx.Response(status_code=status, content=b"ok")


def test_get_with_retry_returns_on_first_success():
    with patch("src.etl._http_retry.httpx.get", return_value=_resp(200)) as g, \
            patch("src.etl._http_retry.time.sleep") as sleep:
        resp = get_with_retry("https://example.test/x")
    assert resp.status_code == 200
    assert g.call_count == 1
    sleep.assert_not_called()


def test_get_with_retry_retries_on_transport_error_then_succeeds():
    side_effects = [
        httpx.ConnectTimeout("first attempt times out"),
        _resp(200),
    ]
    with patch("src.etl._http_retry.httpx.get", side_effect=side_effects) as g, \
            patch("src.etl._http_retry.time.sleep") as sleep:
        resp = get_with_retry("https://example.test/x", max_attempts=3, base_delay=1.0)
    assert resp.status_code == 200
    assert g.call_count == 2
    assert sleep.call_count == 1


def test_get_with_retry_retries_on_5xx_then_succeeds():
    side_effects = [_resp(502), _resp(200)]
    with patch("src.etl._http_retry.httpx.get", side_effect=side_effects) as g, \
            patch("src.etl._http_retry.time.sleep"):
        resp = get_with_retry("https://example.test/x", max_attempts=3, base_delay=1.0)
    assert resp.status_code == 200
    assert g.call_count == 2


def test_get_with_retry_returns_4xx_without_retrying():
    """4xx is a caller error, not transient."""
    with patch("src.etl._http_retry.httpx.get", return_value=_resp(404)) as g, \
            patch("src.etl._http_retry.time.sleep") as sleep:
        resp = get_with_retry("https://example.test/x")
    assert resp.status_code == 404
    assert g.call_count == 1
    sleep.assert_not_called()


def test_get_with_retry_raises_after_exhausting_attempts_transport():
    side_effects = [httpx.ConnectTimeout("nope")] * 3
    with patch("src.etl._http_retry.httpx.get", side_effect=side_effects) as g, \
            patch("src.etl._http_retry.time.sleep"):
        with pytest.raises(httpx.ConnectTimeout):
            get_with_retry("https://example.test/x", max_attempts=3, base_delay=1.0)
    assert g.call_count == 3


def test_get_with_retry_returns_last_response_after_exhausting_5xx():
    """If every attempt is 5xx, we return the last response rather
    than synthesising an error. The caller's raise_for_status() will
    turn it into the right exception."""
    side_effects = [_resp(503), _resp(502), _resp(500)]
    with patch("src.etl._http_retry.httpx.get", side_effect=side_effects), \
            patch("src.etl._http_retry.time.sleep"):
        resp = get_with_retry("https://example.test/x", max_attempts=3, base_delay=1.0)
    assert resp.status_code == 500


def test_call_with_retry_retries_then_succeeds():
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectTimeout("first time")
        return "ok"

    with patch("src.etl._http_retry.time.sleep"):
        result = call_with_retry(flaky, max_attempts=3, base_delay=1.0)
    assert result == "ok"
    assert calls["n"] == 2


def test_call_with_retry_raises_after_exhausting():
    def always_fail() -> str:
        raise httpx.ConnectTimeout("never")

    with patch("src.etl._http_retry.time.sleep"):
        with pytest.raises(httpx.ConnectTimeout):
            call_with_retry(always_fail, max_attempts=2, base_delay=1.0)


# ── RateLimiter ────────────────────────────────────────────────────


def test_rate_limiter_first_wait_is_immediate():
    """First call has no prior call → no sleep."""
    limiter = RateLimiter(min_interval_s=10.0)
    with patch("src.etl._http_retry.time.sleep") as sleep:
        limiter.wait()
    sleep.assert_not_called()


def test_rate_limiter_second_wait_sleeps_remaining_interval(monkeypatch):
    """Two back-to-back waits → sleep for ~min_interval seconds."""
    limiter = RateLimiter(min_interval_s=10.0)
    # Drive monotonic clock manually: first call at t=100, second at t=102.
    times = iter([100.0, 102.0, 102.0])
    monkeypatch.setattr(
        "src.etl._http_retry.time.monotonic", lambda: next(times),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        "src.etl._http_retry.time.sleep", sleeps.append,
    )
    limiter.wait()  # t=100, no sleep
    limiter.wait()  # t=102, elapsed=2, must sleep 8
    assert sleeps == pytest.approx([8.0])


def test_rate_limiter_per_minute_factory():
    limiter = RateLimiter.per_minute(6)   # 1 every 10s
    assert limiter.min_interval_s == pytest.approx(10.0)


@pytest.mark.parametrize("bad", [-1, -0.001])
def test_rate_limiter_rejects_negative_interval(bad):
    with pytest.raises(ValueError):
        RateLimiter(min_interval_s=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_rate_limiter_per_minute_rejects_non_positive(bad):
    with pytest.raises(ValueError):
        RateLimiter.per_minute(bad)


# ── rate_limiter wiring in retry helpers ──────────────────────────


def test_get_with_retry_calls_limiter_before_each_attempt():
    """The limiter must fire before EVERY attempt — retries included —
    so the actual upstream-request rate is governed, not just the
    first-shot rate.
    """
    limiter = MagicMock(spec=RateLimiter)
    side_effects = [httpx.ConnectTimeout("t1"), _resp(200)]
    with patch("src.etl._http_retry.httpx.get", side_effect=side_effects), \
            patch("src.etl._http_retry.time.sleep"):
        resp = get_with_retry(
            "https://example.test/x", max_attempts=3, base_delay=1.0,
            rate_limiter=limiter,
        )
    assert resp.status_code == 200
    # One wait() per attempt — 2 attempts total.
    assert limiter.wait.call_count == 2


def test_call_with_retry_calls_limiter_before_each_attempt():
    limiter = MagicMock(spec=RateLimiter)
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectTimeout("nope")
        return "ok"

    with patch("src.etl._http_retry.time.sleep"):
        out = call_with_retry(
            flaky, max_attempts=5, base_delay=1.0, rate_limiter=limiter,
        )
    assert out == "ok"
    assert limiter.wait.call_count == 3
