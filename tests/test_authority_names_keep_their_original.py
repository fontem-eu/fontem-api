"""A translated authority name keeps the name the authority published.

Contract and grant titles shown in translation always carry the original
(title_original), and the page offers it on hover. Authority names
(name_<lang>, written by the translator) were shown in translation with
nothing beside them: a reader of the German page saw
"Straßen- und Autobahndirektion" and could not find "Ředitelství silnic a
dálnic", the name on every document the authority signs. Wherever a name
is translated, the published one now comes with it; where it is not,
there is nothing to add.
"""
# pylint: disable=missing-function-docstring
from __future__ import annotations

from unittest.mock import MagicMock

from src.data.graph.graph_contract_source import GraphContractSource
from tests.dishka_fixtures import cleanup_dishka, make_test_client

PUBLISHED = "Ředitelství silnic a dálnic"
GERMAN = "Straßen- und Autobahndirektion"


def _row(**over):
    row = {"notice_id": "n1", "publication_number": "1-2025", "title": "Oprava silnice",
           "title_original": None, "value_eur": 1.0e6, "award_date": "2025-03-01",
           "cpv": "45233142", "cpv_description": None, "procedure_type": "open",
           "ted_url": None, "authority": GERMAN, "authority_published": PUBLISHED,
           "authority_id": "auth-rsd", "authority_country": "CZE", "contractor": "STRABAG",
           "contractor_country": "CZE", "contractor_gmr_id": "g1"}
    return {**row, **over}


def _client(single, rows):
    session = MagicMock()
    session.run.return_value.single.return_value = single
    session.run.return_value.data.return_value = rows
    neo4j = MagicMock()
    neo4j.session.return_value.__enter__.return_value = session
    neo4j.session.return_value.__exit__.return_value = False
    return make_test_client(neo4j_client=neo4j, contract_source=GraphContractSource(neo4j))


def _get(client, path, **params):
    try:
        r = client.get(path, params=params)
        assert r.status_code == 200, r.text
        return r.json()
    finally:
        cleanup_dishka()


def test_a_companys_contracts_name_their_buyer_in_translation_and_as_published():
    client = _client({"name": "STRABAG", "country": "CZE", "total": 1.0e6, "cnt": 1}, [_row()])
    (contract,) = _get(client, "/companies/g1/contracts", lang="de")["contracts"]
    assert contract["authority"] == GERMAN and contract["authority_original"] == PUBLISHED


def test_a_buyer_whose_name_is_not_translated_has_no_original_beside_it():
    client = _client({"name": "STRABAG", "country": "CZE", "total": 1.0e6, "cnt": 1},
                     [_row(authority=PUBLISHED)])
    (contract,) = _get(client, "/companies/g1/contracts", lang="fr")["contracts"]
    assert contract["authority"] == PUBLISHED and contract["authority_original"] is None


def test_the_authority_page_heading_keeps_the_published_name():
    client = _client({"name": GERMAN, "published": PUBLISHED, "country": "CZE",
                      "total": 0, "cnt": 0}, [])
    page = _get(client, "/authorities/auth-rsd", lang="de")
    assert page["authority_name"] == GERMAN and page["authority_name_original"] == PUBLISHED


def test_the_contract_page_names_its_buyer_as_published_too():
    session = MagicMock()
    session.run.return_value.single.return_value = {
        "ct": {"ted_notice_id": "n1", "title": "Oprava silnice"},
        "a": {"authority_id": "auth-rsd", "name": PUBLISHED, "name_de": GERMAN},
        "c": None, "cpv": None}
    src = GraphContractSource(MagicMock())
    src._neo4j.session.return_value.__enter__ = MagicMock(  # pylint: disable=protected-access
        return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(  # pylint: disable=protected-access
        return_value=False)
    buyer = src.get_contract_detail("n1", lang="de")["authority"]
    assert buyer["name"] == GERMAN and buyer["name_original"] == PUBLISHED
    assert src.get_contract_detail("n1", lang="fr")["authority"]["name_original"] is None


def test_authorities_found_from_the_header_search_keep_their_published_name():
    session = MagicMock()
    session.run.return_value.data.side_effect = lambda: []
    neo4j = MagicMock()
    neo4j.session.return_value.__enter__.return_value = session

    def run(query, **_params):
        result = MagicMock()
        result.data.return_value = (
            [{"authority_id": "auth-rsd", "name": GERMAN, "published": PUBLISHED,
              "country": "CZE"}] if "MATCH (a:Authority)" in query else [])
        return result
    session.run.side_effect = run
    client = make_test_client(neo4j_client=neo4j)
    (found,) = _get(client, "/search", q="silnic", lang="de")["authorities"]
    assert found["name"] == GERMAN and found["name_original"] == PUBLISHED


def test_the_other_awards_of_a_framework_keep_their_published_titles():
    """The contract page lists a framework's other awards, titled in the
    reader's language: like the contract's own title, each keeps the original."""
    detail = {"ct": {"ted_notice_id": "n1", "title": "Rámcová dohoda", "framework_id": "FA-7"},
              "a": {"authority_id": "auth-rsd", "name": PUBLISHED}, "c": None, "cpv": None}
    sibling = {"ted_notice_id": "n2", "title": "Instandhaltung der Brücken",
               "title_original": "Údržba mostů", "country": "CZE", "value_eur": 2.0e6,
               "publication_date": "2025-02-01", "supplier": "STRABAG"}
    session = MagicMock()

    def run(query, **_params):
        result = MagicMock()
        result.single.return_value = ({"sibling_count": 1} if "sibling_count" in query
                                      else detail)
        result.data.return_value = [sibling]
        return result
    session.run.side_effect = run
    src = GraphContractSource(MagicMock())
    src._neo4j.session.return_value.__enter__ = MagicMock(  # pylint: disable=protected-access
        return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(  # pylint: disable=protected-access
        return_value=False)
    (other,) = src.get_contract_detail("n1", lang="de")["framework"]["siblings"]
    assert other["title"] == "Instandhaltung der Brücken"
    assert other["title_original"] == "Údržba mostů"
