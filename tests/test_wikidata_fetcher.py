"""Unit tests for the Wikidata truthy fetcher.

The HTTP I/O is exercised against a fake httpx transport so we can
pin behaviour for the four outcomes the consumer cares about: OK,
REDIRECT, NOT_FOUND, and "retried 5xx until success". Real network
calls don't belong in unit tests.
"""
from __future__ import annotations

from typing import Callable

import httpx
import pytest
from rdflib import Graph, URIRef

from src.relay.wikidata_fetcher import (
    FetchOutcome,
    _backoff_seconds,
    _extract_redirect_target,
    _retry_after_seconds,
    fetch_truthy,
)


WD = "http://www.wikidata.org/entity/"


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "Test/1"},
    )


_Q42_TTL = (
    b"@prefix wd: <http://www.wikidata.org/entity/> .\n"
    b"@prefix wdt: <http://www.wikidata.org/prop/direct/> .\n"
    b"wd:Q42 wdt:P31 wd:Q5 .\n"
)


def test_fetch_ok_returns_parsed_graph() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_Q42_TTL,
                              headers={"content-type": "text/turtle"})
    client = _client_with(handler)
    result = fetch_truthy("Q42", client)
    assert result.outcome is FetchOutcome.OK
    assert result.entity_id == "Q42"
    assert result.graph is not None
    assert len(result.graph) == 1


def test_fetch_404_returns_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"")
    client = _client_with(handler)
    result = fetch_truthy("Q99999999", client)
    assert result.outcome is FetchOutcome.NOT_FOUND
    assert result.graph is None
    assert result.redirected_to is None


def test_fetch_410_returns_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, content=b"")
    client = _client_with(handler)
    result = fetch_truthy("Q99999999", client)
    assert result.outcome is FetchOutcome.NOT_FOUND


def test_fetch_owl_sameas_in_body_yields_redirect() -> None:
    # 200 OK, but the body asserts the entity is owl:sameAs a
    # different one — this is how Wikidata signals a merged entity
    # without using HTTP 30x.
    body = (
        b"@prefix wd:  <http://www.wikidata.org/entity/> .\n"
        b"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        b"wd:Q1234 owl:sameAs wd:Q5678 .\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)
    client = _client_with(handler)
    result = fetch_truthy("Q1234", client)
    assert result.outcome is FetchOutcome.REDIRECT
    assert result.redirected_to == "Q5678"


def test_fetch_retries_5xx_then_succeeds() -> None:
    # First two calls return 503, third returns the graph.
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, content=b"")
        return httpx.Response(200, content=_Q42_TTL)

    client = _client_with(handler)
    # Backoff is real sleep — for unit-test speed we accept ~3s of
    # sleeps (1 + 2 = 3s). Worth it to exercise the path.
    result = fetch_truthy("Q42", client)
    assert result.outcome is FetchOutcome.OK
    assert calls["n"] == 3


def test_fetch_429_with_retry_after_succeeds_after_one_retry() -> None:
    # Wikimedia returns 429 when we burst too hard. We must NOT
    # treat that as not-found (would silently drop the entity) —
    # respect Retry-After if present, then retry.
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, content=b"",
                                  headers={"Retry-After": "1"})
        return httpx.Response(200, content=_Q42_TTL)

    client = _client_with(handler)
    result = fetch_truthy("Q42", client)
    assert result.outcome is FetchOutcome.OK
    assert calls["n"] == 2


def test_fetch_429_without_retry_after_uses_backoff() -> None:
    # Some 429s arrive without a Retry-After header (especially from
    # a Varnish edge). Fall back to exponential backoff so we still
    # retry instead of dropping.
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, content=b"")
        return httpx.Response(200, content=_Q42_TTL)

    client = _client_with(handler)
    result = fetch_truthy("Q42", client)
    assert result.outcome is FetchOutcome.OK
    assert calls["n"] == 2


def test_fetch_persistent_5xx_raises_after_retries() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"")
    client = _client_with(handler)
    # All 5 retries exhaust; ~31s of sleep. We accept this for the
    # one test; production tooling can mark it slow if it ever
    # becomes a CI bottleneck.
    with pytest.raises(RuntimeError, match="failed to fetch"):
        fetch_truthy("Q42", client)


def test_extract_redirect_target_returns_none_when_no_sameas() -> None:
    g = Graph()
    g.add((URIRef(f"{WD}Q42"),
           URIRef("http://www.w3.org/2000/01/rdf-schema#label"),
           URIRef(f"{WD}Q5")))
    assert _extract_redirect_target(g, "Q42") is None


# ── Helpers extracted from fetch_truthy ───────────────────────────


def test_backoff_seconds_doubles_per_attempt() -> None:
    # Attempt 0 → BASE_BACKOFF_S (1.0s); attempt 3 → 8.0s.
    assert _backoff_seconds(0) == 1.0
    assert _backoff_seconds(1) == 2.0
    assert _backoff_seconds(3) == 8.0


def test_retry_after_seconds_parses_integer_header() -> None:
    resp = httpx.Response(429, headers={"Retry-After": "7"})
    assert _retry_after_seconds(resp, attempt=3) == 7.0


def test_retry_after_seconds_falls_back_to_backoff_when_missing() -> None:
    # No header → exponential backoff for the given attempt index.
    resp = httpx.Response(429)
    assert _retry_after_seconds(resp, attempt=2) == _backoff_seconds(2)


def test_retry_after_seconds_falls_back_to_backoff_on_bad_format() -> None:
    # HTTP-date form is valid per RFC 9110 but unused by our path;
    # garbage / non-numeric → fall back to backoff.
    resp = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert _retry_after_seconds(resp, attempt=1) == _backoff_seconds(1)


# ── 3xx redirect path on fetch_truthy ─────────────────────────────


def test_fetch_3xx_with_location_follows_and_returns_redirect() -> None:
    """Wikidata redirects merged entities to their survivor. The
    follow-up GET to the Location URL returns 200 + owl:sameAs in
    the body, which becomes the REDIRECT FetchResult."""
    survivor_url = "https://www.wikidata.org/wiki/Special:EntityData/Q5.ttl?flavor=simple"
    body = (
        b"@prefix wd: <http://www.wikidata.org/entity/> .\n"
        b"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        b"wd:Q1 owl:sameAs wd:Q5 .\n"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("Q1.ttl") and "Location" not in (req.headers.get("Accept") or ""):
            return httpx.Response(302, headers={"Location": survivor_url})
        return httpx.Response(200, content=body, headers={"content-type": "text/turtle"})

    client = _client_with(handler)
    try:
        result = fetch_truthy("Q1", client)
    finally:
        client.close()

    assert result.outcome is FetchOutcome.REDIRECT
    assert result.entity_id == "Q1"
    assert result.redirected_to == "Q5"


def test_fetch_3xx_without_location_returns_not_found() -> None:
    """A redirect without a Location header is unrecoverable —
    treat as not-found rather than retrying forever."""
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(301)

    client = _client_with(handler)
    try:
        result = fetch_truthy("Q1", client)
    finally:
        client.close()

    assert result.outcome is FetchOutcome.NOT_FOUND


def test_fetch_3xx_followup_non_200_returns_not_found() -> None:
    """If the redirect target itself returns 4xx, give up on the
    entity — no point retrying the same broken hop."""
    target = "https://www.wikidata.org/wiki/Special:EntityData/Q5.ttl?flavor=simple"

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url) == target:
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": target})

    client = _client_with(handler)
    try:
        result = fetch_truthy("Q1", client)
    finally:
        client.close()

    assert result.outcome is FetchOutcome.NOT_FOUND


def test_fetch_unexpected_4xx_treated_as_not_found() -> None:
    """Non-404/410 4xx codes (e.g. 403) — log + treat as not-found
    rather than retry forever. The path is the same as 404 + 410
    but worth pinning the contract."""
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = _client_with(handler)
    try:
        result = fetch_truthy("Q1", client)
    finally:
        client.close()

    assert result.outcome is FetchOutcome.NOT_FOUND


def test_fetch_network_error_then_succeeds() -> None:
    """Transport-level failure on attempt 1 → backoff + retry, then
    the second attempt succeeds normally."""
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("network gone")
        return httpx.Response(200, content=_Q42_TTL, headers={"content-type": "text/turtle"})

    client = _client_with(handler)
    try:
        result = fetch_truthy("Q42", client)
    finally:
        client.close()

    assert result.outcome is FetchOutcome.OK
    assert calls["n"] == 2
