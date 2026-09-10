"""The consolidator review proxy.

Two things are under test. The gate: these four routes are the admin
review screen's, and they were reachable by anyone on the internet
before this proxy existed. And the allowlist: the reason the proxy
declares four routes instead of forwarding a wildcard is that the
consolidator's other endpoints — /resolve, /consolidate/batch, the
Neo4j webhook — must NOT become reachable through this new door.
"""
from __future__ import annotations

import time

import httpx
import jwt
import pytest

from src.api.admin_auth import JWT_ALGORITHM
from src.api.routers import consolidator_review


SECRET = "test-secret-" + "x" * 52


def _token(claims):
    base = {"sub": "u1", "exp": int(time.time()) + 300}
    return jwt.encode({**base, **claims}, SECRET, algorithm=JWT_ALGORITHM)


def _admin(email="admin@fontem.eu"):
    return {"Authorization": "Bearer " + _token(
        {"roles": ["admin"], "email": email})}


@pytest.fixture(name="client")
def _client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    from tests.dishka_fixtures import (  # pylint: disable=import-outside-toplevel
        cleanup_dishka, make_test_client,
    )
    made = make_test_client()
    yield made
    cleanup_dishka()


class _FakeUpstream:
    """Stands in for the consolidator, recording what it was sent."""

    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload if payload is not None else []
        self.calls = []

    def install(self, monkeypatch):
        outer = self

        class _Client:
            def __init__(self, *_a, **_kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def request(self, method, url, params=None, json=None):
                outer.calls.append(
                    {"method": method, "url": url, "params": params, "json": json})
                return httpx.Response(
                    outer.status, json=outer.payload,
                    request=httpx.Request(method, url))

        monkeypatch.setattr(consolidator_review.httpx, "AsyncClient", _Client)
        return self


class TestTheGate:
    """Every route is an operator surface and none may be anonymous."""

    @pytest.mark.parametrize("method,path", [
        ("get", "/consolidator/candidates"),
        ("get", "/consolidator/relationships"),
        ("post", "/consolidator/candidates/a/b/decide"),
        ("post", "/consolidator/relationships/e1/decide"),
    ])
    def test_no_token_is_refused(self, client, method, path):
        kwargs = {"json": {}} if method == "post" else {}
        res = getattr(client, method)(path, **kwargs)
        assert res.status_code in (401, 403)

    def test_a_signed_in_non_admin_is_refused(self, client):
        head = {"Authorization": "Bearer " + _token(
            {"roles": ["editor"], "trust_level": "trusted"})}
        res = client.post(
            "/consolidator/candidates/a/b/decide", json={}, headers=head)
        assert res.status_code == 403

    def test_a_token_from_another_secret_is_refused(self, client):
        forged = jwt.encode(
            {"sub": "u1", "roles": ["admin"], "exp": int(time.time()) + 300},
            "not-ours-" + "y" * 55, algorithm=JWT_ALGORITHM)
        res = client.get(
            "/consolidator/candidates",
            headers={"Authorization": "Bearer " + forged})
        assert res.status_code == 401


class TestTheAllowlist:
    """The proxy must not become a hole of its own.

    These are the consolidator's machine routes. They are reachable
    inside the cluster and must stay that way, but nothing may reach
    them through the public edge — including through here.
    """

    @pytest.mark.parametrize("path", [
        "/consolidator/resolve",
        "/consolidator/resolve/batch",
        "/consolidator/consolidate/batch",
        "/consolidator/consolidate/company/gmr-1",
        "/consolidator/events/dispatch",
        "/consolidator/webhooks/neo4j-trigger",
    ])
    def test_machine_routes_are_not_proxied(self, client, path):
        res = client.post(path, json={}, headers=_admin())
        assert res.status_code == 404, (
            f"{path} is reachable through the review proxy — the wildcard "
            "the module warns about has crept back in")


class TestForwarding:
    """An admin's call reaches the consolidator intact."""

    def test_the_query_string_is_passed_through(self, client, monkeypatch):
        up = _FakeUpstream(payload=[{"from_id": "a"}]).install(monkeypatch)
        res = client.get(
            "/consolidator/candidates?entity_type=Company&limit=100",
            headers=_admin())
        assert res.status_code == 200
        assert res.json() == [{"from_id": "a"}]
        assert up.calls[0]["params"] == {"entity_type": "Company", "limit": "100"}
        assert up.calls[0]["url"].endswith("/candidates")

    def test_the_decision_reaches_the_right_upstream_path(self, client, monkeypatch):
        up = _FakeUpstream(payload={"ok": True}).install(monkeypatch)
        res = client.post(
            "/consolidator/candidates/gmr-1/gmr-2/decide",
            json={"decision": "approve"}, headers=_admin())
        assert res.status_code == 200
        assert up.calls[0]["url"].endswith("/candidates/gmr-1/gmr-2/decide")
        assert up.calls[0]["json"]["decision"] == "approve"

    def test_upstream_status_codes_survive(self, client, monkeypatch):
        """409 means 'already settled'. Flattening it would make the
        screen tell the reviewer something untrue."""
        _FakeUpstream(status=409, payload={"detail": "already declined"}).install(
            monkeypatch)
        res = client.post(
            "/consolidator/candidates/a/b/decide",
            json={"decision": "approve"}, headers=_admin())
        assert res.status_code == 409
        assert res.json()["detail"] == "already declined"

    def test_an_unreachable_consolidator_is_a_502(self, client, monkeypatch):
        class _Broken:
            def __init__(self, *_a, **_kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def request(self, *_a, **_kw):
                raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(consolidator_review.httpx, "AsyncClient", _Broken)
        res = client.get("/consolidator/candidates", headers=_admin())
        assert res.status_code == 502
        assert "unreachable" in res.json()["detail"]


class TestTheReviewerIsTheTokenHolder:
    """The audit log used to record whatever the browser typed."""

    def test_the_reviewer_comes_from_the_token(self, client, monkeypatch):
        up = _FakeUpstream(payload={"ok": True}).install(monkeypatch)
        client.post(
            "/consolidator/candidates/a/b/decide",
            json={"decision": "approve", "reviewer": "someone else"},
            headers=_admin(email="real.admin@fontem.eu"))
        assert up.calls[0]["json"]["reviewer"] == "real.admin@fontem.eu"

    def test_a_client_supplied_reviewer_cannot_win(self, client, monkeypatch):
        """Overwriting, not defaulting — otherwise the audit trail is
        still whatever the client claimed."""
        up = _FakeUpstream(payload={"ok": True}).install(monkeypatch)
        client.post(
            "/consolidator/relationships/e1/decide",
            json={"decision": "accept", "reviewer": "impostor"},
            headers=_admin(email="real.admin@fontem.eu"))
        assert up.calls[0]["json"]["reviewer"] == "real.admin@fontem.eu"
