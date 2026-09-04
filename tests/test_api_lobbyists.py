"""Tests for /lobbyists/{disclosure_id}.

The node shapes here are the ones prod actually holds — checked against
the graph rather than invented, because the load-bearing detail (these
nodes carry disclosure_id and nothing else that identifies them) is
exactly what earlier code got wrong.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring

from tests.dishka_fixtures import (
    _FakeNeo4jClient,
    _FakeNeo4jSession,
    _FakeResult,
    cleanup_dishka,
    make_test_client,
)


# A real registrant, trimmed to the properties the route reads.
JANE_STREET = {
    "disclosure_id": "763743132433-49",
    "detail_name": "Jane Street Group",
    "detail_category": "Companies & groups",
    "detail_country": "UNITED STATES",
    "detail_cost_min": 10000,
    "detail_cost_max": 24999,
    "detail_website": "http://www.janestreet.com/",
    "url": "http://www.janestreet.com/",
    "detail_members_fte": 0.3,
}


class _Row(_FakeResult):
    def __init__(self, row):
        self._row = row

    def single(self):
        return self._row


class _Session(_FakeNeo4jSession):
    """Answers only when the query keys on disclosure_id."""

    def __init__(self, node, filed_for=None):
        self._node = node
        self._filed_for = filed_for or []

    def run(self, query, **kwargs):  # type: ignore[override]
        if self._node is None or kwargs.get("did") != self._node.get("disclosure_id"):
            return _Row(None)
        if "disclosure_id: $did" not in query:
            return _Row(None)
        return _Row({"lobbyist": self._node, "filed_for": self._filed_for})


class _Neo4j(_FakeNeo4jClient):
    def __init__(self, node, filed_for=None):
        self._node = node
        self._filed_for = filed_for

    def session(self):
        return _Session(self._node, self._filed_for)


def test_returns_the_registrant_profile():
    client = make_test_client(neo4j_client=_Neo4j(JANE_STREET))
    try:
        r = client.get("/lobbyists/763743132433-49")
        assert r.status_code == 200
        d = r.json()
        assert d["name"] == "Jane Street Group"
        assert d["disclosure_id"] == "763743132433-49"
        assert d["category"] == "Companies & groups"
    finally:
        cleanup_dishka()


def test_declared_spend_is_a_band_not_a_figure():
    # The register collects a range; flattening it to one number would
    # state a precision the source does not have.
    client = make_test_client(neo4j_client=_Neo4j(JANE_STREET))
    try:
        d = client.get("/lobbyists/763743132433-49").json()
        assert d["declared_spend"] == {
            "min_eur": 10000, "max_eur": 24999, "currency": "EUR",
        }
    finally:
        cleanup_dishka()


def test_a_half_open_band_still_reports():
    # "at least 10M declared" is information; requiring both ends would
    # throw it away.
    node = {**JANE_STREET, "detail_cost_max": None}
    client = make_test_client(neo4j_client=_Neo4j(node))
    try:
        d = client.get("/lobbyists/763743132433-49").json()
        assert d["declared_spend"] == {
            "min_eur": 10000, "max_eur": None, "currency": "EUR",
        }
    finally:
        cleanup_dishka()


def test_no_declared_spend_is_null_not_zero():
    node = {**JANE_STREET, "detail_cost_min": None, "detail_cost_max": None}
    client = make_test_client(neo4j_client=_Neo4j(node))
    try:
        assert client.get("/lobbyists/763743132433-49").json()["declared_spend"] is None
    finally:
        cleanup_dishka()


def test_links_a_resolved_filer_to_its_company_page():
    filed = [{"label": "Company", "name": "Jane Street Europe", "gmr_id": "abc-123"}]
    client = make_test_client(neo4j_client=_Neo4j(JANE_STREET, filed))
    try:
        d = client.get("/lobbyists/763743132433-49").json()
        assert d["filed_for"] == [{
            "label": "Company",
            "name": "Jane Street Europe",
            "profile": "/company/abc-123",
        }]
    finally:
        cleanup_dishka()


def test_a_filer_without_a_gmr_id_gets_no_dead_profile_link():
    # Offering /company/ with no id is the bug this whole page exists to
    # stop repeating.
    filed = [{"label": "Company", "name": "Unresolved Ltd", "gmr_id": None}]
    client = make_test_client(neo4j_client=_Neo4j(JANE_STREET, filed))
    try:
        d = client.get("/lobbyists/763743132433-49").json()
        assert d["filed_for"] == [{
            "label": "Company", "name": "Unresolved Ltd", "profile": None,
        }]
    finally:
        cleanup_dishka()


def test_the_common_case_is_no_filer_at_all():
    # ~4 in 5 registrants resolve to nothing we hold. The page is still
    # the destination for their cards, so it must render.
    client = make_test_client(neo4j_client=_Neo4j(JANE_STREET, []))
    try:
        d = client.get("/lobbyists/763743132433-49").json()
        assert d["filed_for"] == []
        assert d["name"] == "Jane Street Group"
    finally:
        cleanup_dishka()


def test_optional_match_null_row_is_not_reported_as_a_filer():
    # OPTIONAL MATCH yields one all-null row when nothing matched.
    client = make_test_client(
        neo4j_client=_Neo4j(JANE_STREET, [{"label": None, "name": None, "gmr_id": None}]))
    try:
        assert client.get("/lobbyists/763743132433-49").json()["filed_for"] == []
    finally:
        cleanup_dishka()


def test_unknown_disclosure_id_is_404():
    client = make_test_client(neo4j_client=_Neo4j(JANE_STREET))
    try:
        assert client.get("/lobbyists/does-not-exist").status_code == 404
    finally:
        cleanup_dishka()


def test_register_url_is_kept_apart_from_the_org_website():
    # They happen to coincide for some registrants; they are different
    # claims and the page presents them differently.
    client = make_test_client(neo4j_client=_Neo4j(JANE_STREET))
    try:
        d = client.get("/lobbyists/763743132433-49").json()
        assert "register_url" in d and "website" in d
    finally:
        cleanup_dishka()
