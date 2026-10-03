"""Contract and cohesion-grant titles in the reader's language.

The neo4j sink writes machine translations of a title as ``title_<lang>``
on the :Contract / :Disclosure node (fontem-translator events). Every
surface that shows a contract or a grant shows the translation in the
reader's language when one exists, else the original — and keeps the
original beside it, so a reader can always see what the source published.
"""
# pylint: disable=protected-access
from unittest.mock import MagicMock

from src.api.lang import title_original_expr
from src.api.routers.search import _localise_titles
from src.data.graph.graph_contract_source import GraphContractSource
from tests.dishka_fixtures import cleanup_dishka, make_test_client


def _source_with_session(session):
    src = GraphContractSource(MagicMock())
    src._neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(return_value=False)
    return src


# ── the Cypher fragment ───────────────────────────────────────────────

def test_original_is_null_without_a_language():
    assert title_original_expr("ct", None) == "null"


def test_original_only_when_a_translation_is_shown():
    assert title_original_expr("ct", "de") == (
        "CASE WHEN ct.title_de IS NOT NULL THEN ct.title END")


# ── contract detail ───────────────────────────────────────────────────

def _detail(ct, lang):
    session = MagicMock()
    session.run.return_value.single.return_value = {
        "ct": ct, "a": {"authority_id": "a-1", "name": "Nemocnice"},
        "c": None, "cpv": None}
    return _source_with_session(session).get_contract_detail("n1", lang=lang)


CZECH = {"ted_notice_id": "n1", "title": "Léčivý přípravek ZANUBRUTINIB",
         "title_lang": "cs", "title_en": "Medicinal product ZANUBRUTINIB"}


def test_detail_shows_the_translation_and_keeps_the_original():
    out = _detail(CZECH, "en")
    assert out["title"] == "Medicinal product ZANUBRUTINIB"
    assert out["title_original"] == "Léčivý přípravek ZANUBRUTINIB"
    assert out["title_lang"] == "cs"


def test_detail_without_a_translation_shows_the_original_only():
    out = _detail(CZECH, "fr")
    assert out["title"] == "Léčivý přípravek ZANUBRUTINIB"
    assert out["title_original"] is None


def test_detail_in_the_source_language_shows_the_original():
    # The translator never writes the source language itself.
    assert _detail(CZECH, "cs")["title_original"] is None


def test_detail_names_a_detected_source_language():
    ct = {**CZECH, "title_lang": None, "title_lang_detected": "cs"}
    assert _detail(ct, "en")["title_lang"] == "cs"


# ── contract lists and cohesion grants ────────────────────────────────

def test_contract_lists_select_and_return_the_original():
    session = MagicMock()
    session.run.return_value.single.return_value = {"name": "X", "country": "CZE",
                                                    "total": 0, "cnt": 1}
    session.run.return_value.data.return_value = [{
        "notice_id": "n1", "title": "Medicinal product", "title_original": "Léčivý přípravek",
        "value_eur": 1.0, "award_date": "2026-01-01", "cpv": "33", "procedure_type": "open",
        "ted_url": None, "authority": "Nemocnice", "authority_id": "a-1",
        "authority_country": "CZE", "contractor": None, "contractor_gmr_id": None,
        "contractor_country": None}]
    src = _source_with_session(session)
    for out in (src.get_company_contracts("g1", lang="en"),
                src.get_authority_contracts("a-1", lang="en")):
        assert out["contracts"][0]["title_original"] == "Léčivý přípravek"
    cypher = " ".join(str(c.args[0]) for c in session.run.call_args_list)
    assert "coalesce(ct.title_en, ct.title) AS title" in cypher
    assert "CASE WHEN ct.title_en IS NOT NULL THEN ct.title END AS title_original" in cypher


def test_cohesion_grants_are_selected_in_the_language():
    session = MagicMock()
    session.run.return_value.single.return_value = {
        "name": "X", "country": "HRV", "grant_count": 1, "total_eu": 5.0}
    session.run.return_value.data.return_value = [
        {"title": "Ausbau des Hafens", "title_original": "Upgrading of the port"}]
    out = _source_with_session(session).get_company_cohesion_grants("g1", lang="de")
    assert out["grants"][0]["title_original"] == "Upgrading of the port"
    grants_cypher = session.run.call_args_list[1].args[0]
    assert "coalesce(d.title_de, d.title) AS title" in grants_cypher


def test_cohesion_grants_router_passes_a_whitelisted_language():
    source = MagicMock()
    source.get_company_cohesion_grants.return_value = {"grants": []}
    client = make_test_client(contract_source=source)
    client.get("/companies/g1/cohesion-grants?lang=de-AT")
    client.get("/companies/g1/cohesion-grants?lang=xx")
    cleanup_dishka()
    langs = [c.kwargs["lang"] for c in source.get_company_cohesion_grants.call_args_list]
    assert langs == ["de", None]


# ── the batch lookup ──────────────────────────────────────────────────

def test_lookup_binds_the_property_name_and_buckets_by_key():
    session = MagicMock()
    session.run.return_value.data.side_effect = [
        [{"key": "k1", "title": "Bypass", "original": "Obchvat"}],
        [{"key": "Q1", "title": "Port", "original": "Luka"}],
    ]
    out = _source_with_session(session).get_title_translations(
        "en", contract_keys=["k1", "k1", ""], cohesion_ids=["Q1"])
    assert out == {"contracts": {"k1": {"title": "Bypass", "original": "Obchvat"}},
                   "notices": {},
                   "cohesion": {"Q1": {"title": "Port", "original": "Luka"}}}
    first, second = session.run.call_args_list
    # The language is a bound property name, never part of the text.
    assert first.kwargs == {"prop": "title_en", "keys": ["k1"]}
    assert "title_en" not in first.args[0]
    assert second.kwargs == {"prop": "title_en", "ids": ["Q1"]}


def test_lookup_with_nothing_to_look_up_runs_nothing():
    session = MagicMock()
    out = _source_with_session(session).get_title_translations("en")
    assert out == {"contracts": {}, "notices": {}, "cohesion": {}}
    session.run.assert_not_called()


def test_translations_endpoint():
    source = MagicMock()
    source.get_title_translations.return_value = {
        "contracts": {"k1": {"title": "Bypass", "original": "Obchvat"}},
        "notices": {}, "cohesion": {}}
    client = make_test_client(contract_source=source)
    ok = client.post("/translations/titles", json={"lang": "EN", "contract_keys": ["k1"]})
    unknown = client.post("/translations/titles", json={"lang": "klingon", "contract_keys": ["k1"]})
    too_many = client.post("/translations/titles",
                           json={"lang": "en", "contract_keys": [str(i) for i in range(501)]})
    cleanup_dishka()
    assert ok.json()["contracts"]["k1"]["title"] == "Bypass"
    assert source.get_title_translations.call_args.args == ("en",)
    assert unknown.json() == {"lang": None, "contracts": {}, "notices": {}, "cohesion": {}}
    assert source.get_title_translations.call_count == 1
    assert too_many.status_code == 422


# ── search cards ──────────────────────────────────────────────────────

def _results():
    return [
        {"type": "contract", "id": "n1", "title": "Obchvat Klatov"},
        {"type": "eu_cohesion", "id": "Q1", "title": "Upgrading of the port"},
        {"type": "company", "id": "g1", "title": "Skanska"},
        {"type": "contract", "id": "n2", "title": "Untranslated"},
    ]


def test_search_cards_show_translations_and_keep_the_original():
    source = MagicMock()
    source.get_title_translations.return_value = {
        "notices": {"n1": {"title": "Klatovy bypass", "original": "Obchvat Klatov"}},
        "cohesion": {"Q1": {"title": "Ausbau des Hafens", "original": "Upgrading of the port"}}}
    results = _results()
    _localise_titles(results, "de", source)
    assert [r["title"] for r in results] == [
        "Klatovy bypass", "Ausbau des Hafens", "Skanska", "Untranslated"]
    assert results[0]["title_original"] == "Obchvat Klatov"
    assert "title_original" not in results[2] and "title_original" not in results[3]
    assert source.get_title_translations.call_args.kwargs == {
        "notice_ids": ["n1", "n2"], "cohesion_ids": ["Q1"]}


def test_search_without_a_language_looks_nothing_up():
    source = MagicMock()
    results = _results()
    _localise_titles(results, None, source)
    source.get_title_translations.assert_not_called()
    assert results == _results()


def test_search_keeps_its_results_when_the_lookup_fails():
    source = MagicMock()
    source.get_title_translations.side_effect = RuntimeError("neo4j down")
    results = _results()
    _localise_titles(results, "de", source)
    assert results == _results()
