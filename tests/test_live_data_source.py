"""
Regression tests for LiveDataSource
=====================================
• Thundering-herd guard  — concurrent cold-cache misses must only trigger one
  fetch, not N fetches (one per concurrent request).
"""
from __future__ import annotations

import threading
import time

import pandas as pd

from src.cache import FakeRedisCache, CacheConfig
from src.data.live_data_source import LiveDataSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ds(cache: FakeRedisCache) -> LiveDataSource:
    """Return a LiveDataSource wired to the given in-memory cache."""
    cfg = CacheConfig()  # all defaults — no Redis, no network
    return LiveDataSource(cache=cache, cache_config=cfg)


# ---------------------------------------------------------------------------
# Thundering-herd regression
# ---------------------------------------------------------------------------

def test_concurrent_cache_miss_fetches_only_once():
    """
    Regression: before the per-key lock was added, N concurrent requests that
    all saw a cold-cache miss would each independently call the underlying
    fetch function, multiplying expensive network calls by N.

    With the fix, only the first thread should call the fetch; the rest must
    wait and then read the result from cache.
    """
    cache = FakeRedisCache()
    ds = _make_ds(cache)

    fetch_count = 0
    fetch_lock = threading.Lock()

    def _slow_fetch():
        nonlocal fetch_count
        time.sleep(0.05)  # simulate a slow external call
        with fetch_lock:
            fetch_count += 1
        return pd.Series({"2024": 100.0})

    cache_key = "gmretl:prices_THUNDERTEST"
    ttl_key = "ttl_prices"

    results = []
    errors = []

    def _worker():
        try:
            result = ds._get_cached_data(cache_key, _slow_fetch, ttl_key)  # pylint: disable=protected-access
            results.append(result)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Worker threads raised: {errors}"
    assert len(results) == 8, "All threads must get a result"
    assert fetch_count == 1, (
        f"fetch function called {fetch_count} times — thundering herd not prevented"
    )
    # All threads must have received the same value
    for r in results:
        assert r.equals(results[0])


def test_second_key_not_blocked_by_first_key_lock():
    """
    Per-key locking must not serialize fetches for *different* keys.
    Two unrelated cold-cache fetches should be able to run concurrently.
    """
    cache = FakeRedisCache()
    ds = _make_ds(cache)

    started: dict[str, float] = {}

    def _fetch_a():
        started["a"] = time.monotonic()
        time.sleep(0.1)
        return "result_a"

    def _fetch_b():
        started["b"] = time.monotonic()
        time.sleep(0.1)
        return "result_b"

    t_a = threading.Thread(
        target=lambda: ds._get_cached_data("gmretl:key_a", _fetch_a, "ttl_prices")  # pylint: disable=protected-access
    )
    t_b = threading.Thread(
        target=lambda: ds._get_cached_data("gmretl:key_b", _fetch_b, "ttl_prices")  # pylint: disable=protected-access
    )

    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    # Both fetches must have started within ~50 ms of each other (concurrently)
    assert abs(started["a"] - started["b"]) < 0.05, (
        "Fetches for different keys appear to be serialised — lock is too broad"
    )
