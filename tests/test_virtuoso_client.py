"""Unit tests for the SPARQL VirtuosoClient timeout + error contract."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from src.data.sparql.virtuoso_client import (
    SparqlTimeout,
    VirtuosoClient,
)


def test_from_env_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("VIRTUOSO_SPARQL_URL", raising=False)
    assert VirtuosoClient.from_env() is None


def test_from_env_picks_up_url_and_default_timeout(monkeypatch):
    monkeypatch.setenv("VIRTUOSO_SPARQL_URL", "http://virtuoso.local:8890/sparql")
    monkeypatch.delenv("VIRTUOSO_SPARQL_TIMEOUT", raising=False)
    client = VirtuosoClient.from_env()
    assert client is not None
    assert client.sparql_endpoint == "http://virtuoso.local:8890/sparql"
    assert client.timeout == 60.0


def test_from_env_picks_up_explicit_timeout(monkeypatch):
    monkeypatch.setenv("VIRTUOSO_SPARQL_URL", "http://virtuoso.local:8890/sparql")
    monkeypatch.setenv("VIRTUOSO_SPARQL_TIMEOUT", "180")
    client = VirtuosoClient.from_env()
    assert client is not None
    assert client.timeout == 180.0


def test_query_translates_read_timeout_into_sparql_timeout():
    client = VirtuosoClient(
        sparql_endpoint="http://virtuoso.local:8890/sparql", timeout=1.0,
    )

    def _raise(*_args, **_kwargs):
        raise httpx.ReadTimeout("timed out")

    with patch("httpx.Client.get", side_effect=_raise):
        with pytest.raises(SparqlTimeout) as excinfo:
            client.query("SELECT * WHERE { ?s ?p ?o } LIMIT 1")
        assert "1.0" in str(excinfo.value)


def test_query_other_http_errors_are_not_swallowed():
    client = VirtuosoClient(
        sparql_endpoint="http://virtuoso.local:8890/sparql",
    )

    class _Resp:
        status_code = 502

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "bad gateway",
                request=httpx.Request("GET", "http://x/"),
                response=httpx.Response(502),
            )

        def json(self):
            return {}

    with patch("httpx.Client.get", return_value=_Resp()):
        with pytest.raises(httpx.HTTPStatusError):
            client.query("SELECT * WHERE { ?s ?p ?o }")


def test_query_unwraps_typed_literals():
    client = VirtuosoClient(
        sparql_endpoint="http://virtuoso.local:8890/sparql",
    )

    payload = {
        "results": {
            "bindings": [{
                "n": {"value": "42", "datatype": "http://www.w3.org/2001/XMLSchema#integer"},
                "x": {"value": "3.14", "datatype": "http://www.w3.org/2001/XMLSchema#decimal"},
                "s": {"value": "Hello"},
                "iri": {"value": "http://example.com/x", "type": "uri"},
            }],
        },
    }

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return payload

    with patch("httpx.Client.get", return_value=_Resp()):
        rows = client.query("SELECT * WHERE { ?s ?p ?o }")
    assert rows == [{"n": 42, "x": 3.14, "s": "Hello", "iri": "http://example.com/x"}]
