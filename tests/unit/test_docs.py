"""Platform documentation, served to the help page and to the assistant.

Articles have two readers. A person opens them; the model retrieves them to
answer "how do I…" without the manual in every prompt. That second reader is
why they are small — a turn already carries a system prompt, a catalogue,
thirteen tool schemas and history against a 32k window.
"""
from fastapi.testclient import TestClient

from src import docs
from src.api.app import app

client = TestClient(app)


def test_every_manifest_entry_has_a_body():
    """A listing that advertises an article the reader cannot open is worse
    than one that omits it — and for the model it is a dead retrieval."""
    assert docs.missing_bodies() == []


def test_articles_stay_small_enough_to_retrieve():
    """One article is sized to be one RAG chunk. A long one blows the turn's
    budget on a single hit, which is how documentation makes an assistant
    worse rather than better.
    """
    for article in docs.all_articles():
        assert len(article.embedding_text) < 4000, (
            f"{article.id} is {len(article.embedding_text)} chars — split it")


def test_the_listing_omits_bodies():
    """~14k characters of HTML is most of a turn's tool budget, spent before
    the model has chosen what it needs."""
    body = client.get("/help").json()
    for section in body["sections"].values():
        for article in section:
            assert "body" not in article
            assert article["summary"]


def test_the_listing_is_grouped_so_a_model_can_choose():
    body = client.get("/help").json()
    assert body["count"] == len(docs.all_articles())
    assert set(body["sections"]) == {"Data Studio", "Data stores"}


def test_one_article_comes_back_whole():
    r = client.get("/help/studio-plots")
    assert r.status_code == 200
    assert "DuckDB" in r.json()["body"]


def test_an_unknown_id_names_the_alternatives():
    """The caller is often a model that guessed. A bare 404 sends it
    guessing again."""
    r = client.get("/help/does-not-exist")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "studio-overview" in detail


def test_the_prefix_does_not_collide_with_swagger():
    """Mounted at /docs this returned 200 with a page of Swagger HTML: a
    broken endpoint that looks healthy from every angle except the body."""
    assert client.get("/docs").headers["content-type"].startswith("text/html")
    assert client.get("/help").headers["content-type"].startswith("application/json")


def test_both_endpoints_are_offered_to_the_agent():
    """Annotation is the whole mechanism — no separate registration."""
    spec = app.openapi()
    tools = {
        o["x-agent-tool"]["name"]: o["x-agent-tool"]
        for p, ms in spec["paths"].items() if p.startswith("/help")
        for _m, o in ms.items() if "x-agent-tool" in o
    }
    assert set(tools) == {"list_docs", "get_doc"}
    # list_docs is core: a model that never lists cannot discover an id, so
    # get_doc alone is unreachable.
    assert tools["list_docs"]["core"] is True
    assert tools["get_doc"]["params"] == ["article_id"]


def test_embedding_text_leads_with_title_and_summary():
    """A chunk has to carry its own context: 'how do I make a chart' matches
    the framing far better than the middle of a code block."""
    article = docs.get_article("studio-plots")
    assert article.embedding_text.startswith(article.title)
    assert article.summary in article.embedding_text
    assert "<h1>" not in article.embedding_text, "tags would be embedded as noise"


def test_the_documented_schemas_match_the_stores_we_actually_run():
    """These articles are what the model will write queries from. A stale
    table name here is a query that fails for a reason the model cannot see.
    """
    stats = docs.get_article("store-stats").body
    for table in ("observation", "dataset", "nuts_region"):
        assert table in stats
    for column in ("dataset_code", "geo_code", "dimensions", "value"):
        assert column in stats

    graph = docs.get_article("store-graph").body
    for label in ("Company", "Contract", "Authority", "CohesionProject"):
        assert label in graph
    for rel in ("AWARDED_TO", "SUBSIDIARY_OF", "SAME_AS"):
        assert rel in graph
