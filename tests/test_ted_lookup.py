"""Unit tests for the TED publication-number lookup service."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from src.services import ted_lookup
from src.services.ted_lookup import (
    TedLookupError,
    detail_url_for,
    resolve_publication_number,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Tests share an LRU cache by default. Clear before AND after so
    a failure doesn't poison the rest of the suite."""
    resolve_publication_number.cache_clear()
    yield
    resolve_publication_number.cache_clear()


# ── detail_url_for ────────────────────────────────────────────────


def test_detail_url_for_builds_canonical_path():
    assert detail_url_for("295342-2026") == \
        "https://ted.europa.eu/en/notice/-/detail/295342-2026"


# ── resolve_publication_number ────────────────────────────────────


def _mock_post(payload, status=200):
    """Return a context-mgr-compatible httpx.Client whose post() yields
    the given response."""
    resp = httpx.Response(status, json=payload, request=httpx.Request(
        "POST", ted_lookup._TED_SEARCH_URL,
    ))

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, _url, json=None):  # noqa: ARG002
            return resp

    return _Client()


def test_resolve_publication_number_happy_path():
    payload = {
        "notices": [{
            "notice-identifier": "912f1717-1ace-413d-aa61-cd21cd6b95e7",
            "publication-number": "295342-2026",
        }],
    }
    with patch("httpx.Client", return_value=_mock_post(payload)):
        result = resolve_publication_number("912f1717-1ace-413d-aa61-cd21cd6b95e7")
    assert result == "295342-2026"


def test_resolve_publication_number_raises_on_empty_results():
    """TED has no record of the UUID — surface as TedLookupError so
    the router translates to a 404 (not a 500)."""
    with patch("httpx.Client", return_value=_mock_post({"notices": []})):
        with pytest.raises(TedLookupError) as exc:
            resolve_publication_number("00000000-0000-0000-0000-000000000000")
    assert "no published notice" in str(exc.value)


def test_resolve_publication_number_raises_on_missing_pub_number():
    """Match exists but lacks a publication-number — happens when a
    notice is queued but not yet published. Treat as 404 too."""
    payload = {"notices": [{"notice-identifier": "abc"}]}
    with patch("httpx.Client", return_value=_mock_post(payload)):
        with pytest.raises(TedLookupError) as exc:
            resolve_publication_number("abc")
    assert "no publication-number" in str(exc.value)


def test_resolve_publication_number_propagates_http_errors():
    """502 / 500 / connect-error / timeout — all surface as the raw
    httpx exception so the router can return a 502."""

    def _raise(*_args, **_kwargs):
        raise httpx.ConnectError("backend down")

    class _BrokenClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *_args, **_kwargs):
            _raise()

    with patch("httpx.Client", return_value=_BrokenClient()):
        with pytest.raises(httpx.HTTPError):
            resolve_publication_number("abc")


def test_resolve_publication_number_caches_result():
    """Two successive calls for the same UUID hit TED once; the second
    is served from the LRU cache."""
    payload = {
        "notices": [{"publication-number": "295342-2026"}],
    }
    calls = {"n": 0}

    class _CountingClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *_args, **_kwargs):
            calls["n"] += 1
            return httpx.Response(
                200, json=payload, request=httpx.Request("POST", "x"),
            )

    with patch("httpx.Client", return_value=_CountingClient()):
        resolve_publication_number("abc")
        resolve_publication_number("abc")
    assert calls["n"] == 1
