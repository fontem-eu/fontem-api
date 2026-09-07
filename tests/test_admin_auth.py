"""The gate on the operator endpoints.

Until 2026-09-07 `POST /entity-resolution/resolve/...` merged two
entities in the production graph for anyone who asked, and
`POST /value-review/{id}/decide` emitted a corrective event on the same
terms. Both were reachable from the internet because nginx proxies all
of /api/ to this service and nothing here checked a thing.
"""
from __future__ import annotations

import time

import jwt
import pytest
from fastapi import HTTPException

from src.api.admin_auth import JWT_ALGORITHM, is_data_admin, require_data_admin


SECRET = "test-secret"


class _Creds:
    def __init__(self, token):
        self.credentials = token


def _token(claims, secret=SECRET):
    base = {"sub": "u1", "exp": int(time.time()) + 300}
    return jwt.encode({**base, **claims}, secret, algorithm=JWT_ALGORITHM)


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)


class TestIsDataAdmin:
    def test_admin_role_is_enough(self):
        assert is_data_admin({"roles": ["admin"]}) is True

    def test_admin_trust_level_is_enough(self):
        # Mirrors the community API's policy, which accepts either.
        assert is_data_admin({"trust_level": "admin"}) is True

    def test_other_roles_are_not_admin(self):
        # editor and moderator are real roles on the one admin account,
        # so it would be easy to accept them by accident.
        assert is_data_admin({"roles": ["editor", "moderator"]}) is False

    def test_no_claims_is_not_admin(self):
        assert is_data_admin({}) is False

    def test_a_non_list_roles_claim_does_not_crash_or_pass(self):
        # A hand-made token can put anything in the claim.
        assert is_data_admin({"roles": "admin"}) is False


class TestRequireDataAdmin:
    def test_admin_token_is_accepted(self):
        claims = require_data_admin(_Creds(_token({"roles": ["admin"]})))
        assert claims["sub"] == "u1"

    def test_a_signed_in_non_admin_is_refused(self):
        with pytest.raises(HTTPException) as e:
            require_data_admin(_Creds(_token({"roles": ["editor"]})))
        assert e.value.status_code == 403

    def test_a_token_signed_with_another_secret_is_refused(self):
        with pytest.raises(HTTPException) as e:
            require_data_admin(_Creds(_token({"roles": ["admin"]}, secret="not-ours")))
        assert e.value.status_code == 401

    def test_an_expired_token_is_refused(self):
        stale = jwt.encode(
            {"sub": "u1", "roles": ["admin"], "exp": int(time.time()) - 10},
            SECRET, algorithm=JWT_ALGORITHM)
        with pytest.raises(HTTPException) as e:
            require_data_admin(_Creds(stale))
        assert e.value.status_code == 401

    def test_garbage_is_refused(self):
        with pytest.raises(HTTPException) as e:
            require_data_admin(_Creds("not-a-token"))
        assert e.value.status_code == 401

    def test_no_secret_configured_refuses_rather_than_allows(self, monkeypatch):
        """The property that matters most.

        A deployment that forgets to wire JWT_SECRET must fail loudly.
        Defaulting to a development secret would mean silently accepting
        tokens signed with a string published in this repository — a
        quiet reopening of the hole this module closes.
        """
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(HTTPException) as e:
            require_data_admin(_Creds(_token({"roles": ["admin"]})))
        assert e.value.status_code == 503

    def test_empty_secret_is_treated_as_unconfigured(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET", "")
        with pytest.raises(HTTPException) as e:
            require_data_admin(_Creds(_token({"roles": ["admin"]})))
        assert e.value.status_code == 503


class TestTheEndpointsAreActuallyGated:
    """The unit tests above prove the dependency works. These prove it is
    wired to the routes that were open, which is the part that regressed
    into production."""

    @staticmethod
    def _client():
        from tests.dishka_fixtures import make_test_client  # pylint: disable=import-outside-toplevel
        return make_test_client()

    def test_entity_resolution_candidates_needs_a_token(self):
        from tests.dishka_fixtures import cleanup_dishka  # pylint: disable=import-outside-toplevel
        client = self._client()
        try:
            assert client.get("/entity-resolution/candidates").status_code in (401, 403)
        finally:
            cleanup_dishka()

    def test_the_merge_needs_a_token(self):
        """The one that could rewrite the graph."""
        from tests.dishka_fixtures import cleanup_dishka  # pylint: disable=import-outside-toplevel
        client = self._client()
        try:
            res = client.post(
                "/entity-resolution/resolve/dup-1/canon-1", json={"action": "approve"})
            assert res.status_code in (401, 403)
        finally:
            cleanup_dishka()

    def test_the_value_review_decision_needs_a_token(self):
        from tests.dishka_fixtures import cleanup_dishka  # pylint: disable=import-outside-toplevel
        client = self._client()
        try:
            res = client.post("/value-review/1/decide", json={"action": "correct"})
            assert res.status_code in (401, 403)
        finally:
            cleanup_dishka()

    def test_a_public_read_endpoint_is_still_public(self):
        """The gate must not have leaked onto the rest of the API."""
        from tests.dishka_fixtures import cleanup_dishka  # pylint: disable=import-outside-toplevel
        client = self._client()
        try:
            assert client.get("/geo/nuts-regions").status_code == 200
        finally:
            cleanup_dishka()
