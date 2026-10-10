"""Names and titles in the graph explorer, the story side panel and the
spending page, in the reader's language.

The translator writes contract and grant titles as ``title_<lang>`` and
authority names as ``name_<lang>``; contract lists, contract pages and
search show them. These three surfaces labelled every node with what the
source published, whatever the reader's language. With ``lang`` they show
the translation where one exists and keep the original beside it.
"""
# pylint: disable=missing-function-docstring,unused-argument
from __future__ import annotations

from unittest.mock import MagicMock

from src.data.graph.graph_recommendations_source import GraphRecommendationsSource
from tests.dishka_fixtures import cleanup_dishka, make_test_client
from tests.test_api_graph import FakeNeo4jClient, FakeNode, FakePath, FakeRelationship, FakeResult

UID = "0b1f5a86-7a39-5b0c-9d2f-4c7e8a4d1e11"
CONTRACT = FakeNode(["Contract"], {
    "ted_notice_id": "123456-2025", "title": "Oprava silnice I/27",
    "title_lang": "cs", "title_de": "Reparatur der Straße I/27"})
AUTHORITY = FakeNode(["Authority"], {
    "authority_id": "auth-rsd", "name": "Ředitelství silnic a dálnic",
    "name_de": "Straßen- und Autobahndirektion", "country": "CZE"})
COMPANY = FakeNode(["Company"], {"gmr_id": "comp-strabag", "name": "STRABAG a.s."})


def _graph_handler(query, **kwargs):  # pylint: disable=too-many-return-statements
    known = {"comp-strabag": COMPANY, "auth-rsd": AUTHORITY}
    if "labels(n)[0]" in query:
        node = known.get(kwargs.get("eid"))
        return FakeResult({"label": next(iter(node.labels))} if node else None)
    if "RETURN n LIMIT 1" in query:
        node = known.get(kwargs.get("eid"))
        return FakeResult({"n": node} if node else None)
    path = FakePath([COMPANY, CONTRACT, AUTHORITY], [
        FakeRelationship(CONTRACT, COMPANY, "AWARDED_TO"),
        FakeRelationship(AUTHORITY, CONTRACT, "AWARDED")])
    if "shortestPath" in query:
        return FakeResult({"path": path})
    if "RETURN path" in query and "LIMIT 9" not in query:
        return FakeResult([{"path": path}])
    return FakeResult(None)


def _get(path, **params):
    client = make_test_client(neo4j_client=FakeNeo4jClient(_graph_handler))
    try:
        r = client.get(path, params=params)
        assert r.status_code == 200, r.text
        return r.json()
    finally:
        cleanup_dishka()


# ── graph explorer ────────────────────────────────────────────────────

def test_the_explorer_labels_contracts_and_authorities_in_the_readers_language():
    nodes = {n["id"]: n for n in _get("/graph/comp-strabag", depth=2, lang="de")["nodes"]}
    assert nodes["123456-2025"]["label"] == "Reparatur der Straße I/27"
    assert nodes["123456-2025"]["label_original"] == "Oprava silnice I/27"
    assert nodes["auth-rsd"]["label"] == "Straßen- und Autobahndirektion"
    assert nodes["auth-rsd"]["label_original"] == "Ředitelství silnic a dálnic"
    # A company's name is its name in every language.
    assert nodes["comp-strabag"]["label"] == "STRABAG a.s."
    assert nodes["comp-strabag"]["label_original"] is None


def test_the_explorer_without_a_language_or_a_translation_shows_what_was_published():
    for params in ({}, {"lang": "fr"}):
        nodes = {n["id"]: n for n in _get("/graph/comp-strabag", depth=2, **params)["nodes"]}
        assert nodes["123456-2025"]["label"] == "Oprava silnice I/27"
        assert nodes["123456-2025"]["label_original"] is None


def test_a_path_search_names_its_ends_in_the_readers_language():
    body = _get("/graph/paths/find", **{"from": "comp-strabag", "to": "auth-rsd", "lang": "de"})
    assert body["to_node"]["label"] == "Straßen- und Autobahndirektion"
    assert body["to_node"]["label_original"] == "Ředitelství silnic a dálnic"
    assert body["from_node"]["label"] == "STRABAG a.s."


# ── the side panel a story's mention opens ────────────────────────────

class _OneNode:
    def __init__(self, node):
        self._node = node

    def session(self):
        return self

    def run(self, query, **kwargs):
        result = MagicMock()
        result.single.return_value = {"n": self._node}
        return result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def _panel(cls, node, lang=None):
    client = make_test_client(neo4j_client=_OneNode(node))
    try:
        r = client.get("/mentions/resolve", params={
            "iri": f"http://data.fontem.eu/id/{cls}/{UID}", **({"lang": lang} if lang else {})})
        assert r.status_code == 200, r.text
        return r.json()
    finally:
        cleanup_dishka()


def test_a_mentioned_authority_is_named_in_the_readers_language():
    node = {"gmr_id": UID, **dict(AUTHORITY.items())}
    panel = _panel("Authority", node, "de")
    assert panel["label"] == "Straßen- und Autobahndirektion"
    assert panel["label_original"] == "Ředitelství silnic a dálnic"
    assert _panel("Authority", node)["label"] == "Ředitelství silnic a dálnic"


def test_a_mentioned_grant_is_labelled_by_its_title():
    """A cohesion project carries a title, not a name: its panel had no label."""
    node = {"gmr_id": UID, "title": "Modernizace přístavu", "title_de": "Modernisierung des Hafens"}
    assert _panel("CohesionProject", node)["label"] == "Modernizace přístavu"
    assert _panel("CohesionProject", node, "de")["label"] == "Modernisierung des Hafens"


# ── the spending page ─────────────────────────────────────────────────

class _Rows:
    """Answers the top-authorities query with the authority's row as the
    graph would return it for the property asked."""

    def session(self):
        return self

    def run(self, query, params=None, **kwargs):
        params = {**(params or {}), **kwargs}
        result = MagicMock()
        if "MATCH (a:Authority" in query:
            row = {"id": "auth-rsd", "name": "Ředitelství silnic a dálnic",
                   "total_value": 9.5e9, "contract_count": 412}
            prop = params.get("name_prop")
            row["translated"] = dict(AUTHORITY.items()).get(prop) if prop else None
            result.data.return_value = [row]
        else:
            result.data.return_value = []
        return result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _spending(lang=None):
    client = make_test_client(recommendations_source=GraphRecommendationsSource(_Rows()))
    try:
        r = client.get("/euro-tracker/recommendations",
                       params={"country": "CZE", **({"lang": lang} if lang else {})})
        assert r.status_code == 200, r.text
        return r.json()["authorities"]
    finally:
        cleanup_dishka()


def test_the_top_authorities_are_named_in_the_readers_language():
    (top,) = _spending("de")
    assert top["name"] == "Straßen- und Autobahndirektion"
    assert top["name_original"] == "Ředitelství silnic a dálnic"
    assert top["total_value_eur"] == 9.5e9


def test_the_top_authorities_without_a_translation_keep_their_name():
    for lang in (None, "fr"):
        (top,) = _spending(lang)
        assert top["name"] == "Ředitelství silnic a dálnic" and top["name_original"] is None
