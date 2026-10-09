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


def test_the_register_link_opens_the_registrants_register_entry():
    """Regression: the link read the stored `url`, which held the
    organisation's own website for all 17,398 registrants that had one, so
    "EU Transparency Register entry" opened janestreet.com."""
    client = make_test_client(neo4j_client=_Neo4j(JANE_STREET))
    try:
        d = client.get("/lobbyists/763743132433-49").json()
        assert d["register_url"] == (
            "https://transparency-register.europa.eu/search-register-or-update/"
            "organisation-detail_en?id=763743132433-49")
        assert d["website"] == "http://www.janestreet.com/"
    finally:
        cleanup_dishka()


BRAUER = {
    "disclosure_id": "9218245390-27",
    "detail_name": "Beispiel Brauer-Bund e.V.",
    "detail_goals": "Die Interessen der deutschen Brauwirtschaft vertreten.",
    "detail_goals_lang": "de", "detail_goals_lang_origin": "detected",
    "detail_goals_translated_from": "Die Interessen der deutschen Brauwirtschaft vertreten.",
    "detail_goals_en": "Representing the interests of the German brewing industry.",
    "detail_goals_fr": "Représenter les intérêts de la brasserie allemande.",
    "detail_goals_summarized_from": "Die Interessen der deutschen Brauwirtschaft vertreten.",
    "detail_goals_summary_de": "Vertritt die deutsche Brauwirtschaft.",
    "detail_goals_summary_en": "Represents the German brewing industry.",
    "detail_financial_type": "ngo", "detail_total_budget_eur": 1260031,
    "detail_funding_sources": ["Member's contributions", "EU funding"],
    "detail_contributor_names": ["Member breweries", "Foundation Example"],
    "detail_contributor_amounts_eur": [820000, 0],
    "detail_grant_sources": ["EU LIFE"], "detail_grant_amounts_eur": [82484],
}


def _get(node, path):
    client = make_test_client(neo4j_client=_Neo4j(node))
    try:
        return client.get(path).json()
    finally:
        cleanup_dishka()


def test_a_reader_gets_the_goals_and_their_summary_in_their_language():
    d = _get(BRAUER, "/lobbyists/9218245390-27?lang=fr")
    assert d["goals"] == "Représenter les intérêts de la brasserie allemande."
    assert d["goals_original"].startswith("Die Interessen") and d["goals_lang"] == "de"
    assert d["goals_translated"] is True
    # No French summary yet: the one in the goals' own language stands in.
    assert d["goals_summary"] == "Vertritt die deutsche Brauwirtschaft."
    en = _get(BRAUER, "/lobbyists/9218245390-27?lang=en")
    assert en["goals_summary"] == "Represents the German brewing industry."


def test_a_reader_of_the_goals_own_language_gets_them_as_written():
    d = _get(BRAUER, "/lobbyists/9218245390-27?lang=de")
    assert d["goals"] == BRAUER["detail_goals"] and d["goals_translated"] is False
    assert _get(BRAUER, "/lobbyists/9218245390-27")["goals"] == BRAUER["detail_goals"]


def test_a_translation_of_goals_since_rewritten_is_not_shown():
    rewritten = {**BRAUER, "detail_goals": "Wir vertreten heute auch alkoholfreie Getränke."}
    d = _get(rewritten, "/lobbyists/9218245390-27?lang=en")
    assert d["goals"] == "Wir vertreten heute auch alkoholfreie Getränke."
    assert d["goals_translated"] is False and d["goals_summary"] is None


def test_an_unknown_language_is_ignored():
    d = _get(BRAUER, "/lobbyists/9218245390-27?lang=xx;drop")
    assert d["goals"] == BRAUER["detail_goals"]


def test_an_ngos_declared_finances_are_shown_as_declared():
    f = _get(BRAUER, "/lobbyists/9218245390-27")["finances"]
    assert f["type"] == "ngo" and f["total_budget_eur"] == 1260031
    assert f["funding_sources"] == ["Member's contributions", "EU funding"]
    assert f["contributors"] == [{"name": "Member breweries", "amount_eur": 820000},
                                 {"name": "Foundation Example", "amount_eur": 0}]
    assert f["grants"] == [{"source": "EU LIFE", "amount_eur": 82484}]
    assert f["clients"] == [] and f["revenue"] is None
