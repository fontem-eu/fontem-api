"""Petitions and lobbying entries in the reader's language, wherever they show.

The European Citizens' Initiative register publishes each initiative in the
EU's languages (title_<lang>, objectives_<lang> on :Petition); the
Transparency Register publishes a registrant's goals as written, and the
translator adds detail_goals_<lang> and a tweet-long summary in every
language. The petition and lobbyist pages read them in the reader's
language; these tests hold search, and the legislation a petition links
to, to the same: a reader who searches in French finds the French
initiative and what a lobby works for, in French.
"""
# pylint: disable=redefined-outer-name
from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest

from src.data.graph.graph_contract_source import GraphContractSource
from tests.dishka_fixtures import cleanup_dishka, make_test_client

VIDEOGAMES_OBJECTIVES = "Require publishers that sell videogames to leave them playable."
VIDEOGAMES = {
    "system": "eu-eci", "petition_id": "ECI(2024)000007", "status": "ANSWERED",
    "title": "Stop Destroying Videogames", "title_lang": "en",
    "objectives": VIDEOGAMES_OBJECTIVES,
    "title_en": "Stop Destroying Videogames", "objectives_en": VIDEOGAMES_OBJECTIVES,
    "title_fr": "Stop à la destruction des jeux vidéo",
    "objectives_fr": "Obliger les éditeurs à laisser les jeux vidéo jouables.",
    "objectives_summarized_from": VIDEOGAMES_OBJECTIVES,
    "objectives_summary_en": "Asks the EU to keep sold games playable.",
    "objectives_summary_fr": "Demande à l'UE de garder jouables les jeux vendus.",
    "total_supporters": 1294188,
}
BREWERS_GOALS = "Die Interessen der deutschen Brauwirtschaft gegenüber der EU vertreten."
BREWERS = {
    "system": "eu-lobbying", "disclosure_id": "71234567890-12", "title": "Deutscher Brauer-Bund",
    "detail_name": "Deutscher Brauer-Bund", "detail_goals": BREWERS_GOALS,
    "detail_goals_lang": "de", "detail_goals_translated_from": BREWERS_GOALS,
    "detail_goals_fr": "Représenter les intérêts des brasseurs allemands auprès de l'UE.",
    "detail_goals_summarized_from": BREWERS_GOALS,
    "detail_goals_summary_de": "Vertritt die deutsche Brauwirtschaft bei der EU.",
    "detail_goals_summary_fr": "Représente la brasserie allemande auprès de l'UE.",
    "detail_country": "GERMANY", "detail_country_iso": "DEU",
}


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows

    def single(self):
        return self._rows[0] if self._rows else None


class _Graph:
    """The registers' nodes, answered to the queries that ask for them by id."""

    def __init__(self, petitions=(), lobbyists=(), down=False):
        self.petitions = {p["petition_id"]: p for p in petitions}
        self.lobbyists = {l["disclosure_id"]: l for l in lobbyists}
        self.down = down

    def session(self):
        return self

    def run(self, query, **params):
        if self.down:
            raise RuntimeError("neo4j unavailable")
        if "MATCH (p:Petition)" in query and "ids" in params:
            return _Result([{"p": self.petitions[i]} for i in params["ids"]
                            if i in self.petitions])
        if "MATCH (l:Lobbyist)" in query and "ids" in params:
            return _Result([{"l": self.lobbyists[i]} for i in params["ids"]
                            if i in self.lobbyists])
        return _Result([])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


SEARCH_COLUMNS = ["entity_type", "entity_id", "embed_text", "country", "event_date",
                  "rrf_score", "lex_rank", "vec_rank", "nuts", "sector", "meta"]


def _row(etype, eid, text, meta=None):
    return (etype, eid, text, None, None, 0.5, 1, None, None, None, meta or {})


#: What the search index holds for them: the English initiative with its
#: objectives in full, and the registrant's name with the register's tag.
INDEXED = [
    _row("petition", "ECI(2024)000007",
         "Stop Destroying Videogames — Require publishers that sell videogames — "
         "to leave them playable.", {"status": "ANSWERED"}),
    _row("eu_lobbying", "71234567890-12", "Deutscher Brauer-Bund — eu-lobbying",
         {"system": "eu-lobbying"}),
]


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = [MagicMock() for _ in SEARCH_COLUMNS]
        for m, name in zip(self.description, SEARCH_COLUMNS):
            m.name = name

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, *_a, **_k):
        return None

    def fetchall(self):
        return self._rows


class _Conn:
    read_only = False

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def cursor(self):
        return _Cursor(self._rows)


@pytest.fixture
def search(monkeypatch):
    """GET /search/results against an index holding INDEXED and `graph`."""
    monkeypatch.setenv("SEARCH_DATABASE_URL", "postgresql://x:x@localhost:5432/x")
    monkeypatch.delenv("LINGUISTICS_URL", raising=False)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _Conn(INDEXED))

    def run(query, graph):
        client = make_test_client(neo4j_client=graph,
                                  contract_source=GraphContractSource(graph))
        try:
            r = client.get("/search/results", params={"q": query, **_lang_of(query)})
            assert r.status_code == 200, r.text
            return {c["type"]: c for c in r.json()["results"]}
        finally:
            cleanup_dishka()
    return run


def _lang_of(query):
    return {"lang": query.split(":", 1)[0]} if ":" in query else {}


# ── search ────────────────────────────────────────────────────────────

def test_a_reader_searching_in_french_finds_the_french_initiative_and_its_summary(search):
    cards = search("fr:jeux vidéo", _Graph(petitions=[VIDEOGAMES]))
    card = cards["petition"]
    assert card["title"] == "Stop à la destruction des jeux vidéo"
    assert card["title_original"] == "Stop Destroying Videogames"
    assert card["subtitle"] == "Demande à l'UE de garder jouables les jeux vendus."
    assert card["subtitle_is_summary"] is True and card["context"] == ""


def test_a_lobby_found_by_search_says_what_it_works_for_in_the_readers_language(search):
    card = search("fr:brasseurs", _Graph(lobbyists=[BREWERS]))["eu_lobbying"]
    assert card["title"] == "Deutscher Brauer-Bund"
    assert card["subtitle"] == "Représente la brasserie allemande auprès de l'UE."
    assert card["subtitle_is_summary"] is True


def test_without_a_language_the_summaries_come_in_their_own_language(search):
    cards = search("videogames brewers", _Graph(petitions=[VIDEOGAMES], lobbyists=[BREWERS]))
    assert cards["petition"]["title"] == "Stop Destroying Videogames"
    assert "title_original" not in cards["petition"]
    assert cards["petition"]["subtitle"] == "Asks the EU to keep sold games playable."
    assert cards["eu_lobbying"]["subtitle"] == "Vertritt die deutsche Brauwirtschaft bei der EU."


def test_a_language_with_no_version_or_summary_falls_back_to_the_published_one(search):
    cards = search("pl:gry", _Graph(petitions=[VIDEOGAMES], lobbyists=[BREWERS]))
    assert cards["petition"]["title"] == "Stop Destroying Videogames"
    assert cards["petition"]["subtitle"] == "Asks the EU to keep sold games playable."
    assert cards["eu_lobbying"]["subtitle"] == "Vertritt die deutsche Brauwirtschaft bei der EU."


def test_a_summary_of_goals_since_rewritten_is_not_shown_and_nor_is_the_registers_tag(search):
    rewritten = {**BREWERS, "detail_goals": "Ganz andere Ziele, seit gestern."}
    card = search("fr:brasseurs", _Graph(lobbyists=[rewritten]))["eu_lobbying"]
    assert card["subtitle"] == "" and "subtitle_is_summary" not in card


def test_search_keeps_its_cards_when_the_graph_cannot_answer(search):
    cards = search("fr:jeux", _Graph(petitions=[VIDEOGAMES], down=True))
    assert cards["petition"]["title"] == "Stop Destroying Videogames"
    assert cards["eu_lobbying"]["title"] == "Deutscher Brauer-Bund"


# ── the legislation a petition links to ───────────────────────────────

ACT = {"celex": "32024D1824", "title_en": "Commission Implementing Decision (EU) 2024/1824",
       "title_fr": "Décision d'exécution (UE) 2024/1824 de la Commission",
       "date_document": "2024-06-17", "doc_type": "Decision"}


def _petition_page(lang):
    graph = MagicMock()
    graph.session.return_value.__enter__.return_value.run.return_value.data.return_value = [
        {"petition": VIDEOGAMES, "acts": [{"rel": "REGISTERED_BY", "act": ACT},
                                          {"rel": None, "act": None}]}]
    client = make_test_client(neo4j_client=graph)
    try:
        r = client.get("/petitions/detail", params={"petition_id": "ECI(2024)000007",
                                                    **({"lang": lang} if lang else {})})
        assert r.status_code == 200, r.text
        return r.json()["legislation"]
    finally:
        cleanup_dishka()


def test_linked_legislation_is_titled_in_the_readers_language_and_opens_there():
    (act,) = _petition_page("fr")
    assert act["title"] == "Décision d'exécution (UE) 2024/1824 de la Commission"
    assert act["title_lang"] == "fr" and act["rel"] == "REGISTERED_BY"
    assert act["eurlex_url"] == ("https://eur-lex.europa.eu/legal-content/FR/TXT/"
                                 "?uri=CELEX:32024D1824")


def test_legislation_without_a_title_in_the_language_shows_english_but_opens_in_it():
    """EUR-Lex publishes every act in all 24 languages; we hold two titles."""
    (act,) = _petition_page("de")
    assert act["title"] == "Commission Implementing Decision (EU) 2024/1824"
    assert act["title_lang"] == "en"
    assert act["eurlex_url"].startswith("https://eur-lex.europa.eu/legal-content/DE/TXT/")


def test_legislation_without_a_language_is_in_english():
    (act,) = _petition_page(None)
    assert act["title_lang"] == "en" and "/EN/TXT/" in act["eurlex_url"]


# ── the lobbyist page ─────────────────────────────────────────────────

def _lobbyist_page(node, lang):
    graph = MagicMock()
    graph.session.return_value.__enter__.return_value.run.return_value.single.return_value = {
        "lobbyist": node, "filed_for": []}
    client = make_test_client(neo4j_client=graph)
    try:
        r = client.get(f"/lobbyists/{node['disclosure_id']}", params={"lang": lang})
        assert r.status_code == 200, r.text
        return r.json()
    finally:
        cleanup_dishka()


def test_the_page_says_which_language_the_summary_is_in():
    assert _lobbyist_page(BREWERS, "fr")["goals_summary_lang"] == "fr"
    # No summary in Polish: the one in the goals' own language stands, and says so.
    page = _lobbyist_page(BREWERS, "pl")
    assert page["goals_summary"] == "Vertritt die deutsche Brauwirtschaft bei der EU."
    assert page["goals_summary_lang"] == "de"


def test_the_country_comes_with_a_code_any_language_can_name():
    page = _lobbyist_page(BREWERS, "fr")
    assert page["country"] == "GERMANY" and page["country_code"] == "DE"
    assert _lobbyist_page({**BREWERS, "detail_country_iso": None}, "fr")["country_code"] is None
