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
    _extract_redirect_target,
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
