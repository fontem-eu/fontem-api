"""Tests for the faceted graph search endpoint (GET /search/results)."""
import pytest

from tests.dishka_fixtures import make_test_client, cleanup_dishka


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows

    def single(self):
        return self._rows[0] if self._rows else None


class _Session:
    """Fake Neo4j session: returns canned rows keyed by a query substring."""

    def __init__(self, rowmap):
        self._rowmap = rowmap
        self.queries = []

    def run(self, query, **_params):
        self.queries.append(query)
        for anchor, rows in self._rowmap.items():
            if anchor in query:
                return _Result(rows)
        return _Result([])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Neo4j:
    def __init__(self, rowmap):
        self._session = _Session(rowmap)

    def session(self):
        return self._session

    def close(self):
        pass


# Canned rows matching each handler's RETURN aliases.
FULL_ROWMAP = {
    "MATCH (c:Company)": [
        {"id": "c1", "title": "Apple Inc.", "country": "USA", "ticker": "AAPL",
         "legal_form": "Inc.", "city": "Cupertino", "rank": 3},
        {"id": "c2", "title": "Pineapple Power", "country": "GBR", "ticker": None,
         "legal_form": None, "city": None, "rank": 0},
    ],
    "MATCH (a:Authority)": [
        {"id": "a1", "title": "Apple Authority", "country": "ITA", "rank": 2},
    ],
    "MATCH (p:Person)": [
        {"id": "p1", "title": "Jane Apple", "birth_year": 1970,
         "companies": ["Apple Inc."]},
    ],
    "MATCH (l:Lobbyist)": [
        {"id": "l1", "title": "Apple Lobby", "acronym": "AL", "country": "BEL",
         "category": "In-house", "reg_date": "2021-03-01", "goals": None,
         "url": "www.apple-lobby.eu"},
    ],
    "MATCH (ct:Contract)": [
        {"id": "t1", "title": "Apple procurement", "country": "PRT",
         "pub_date": "2022-05-01", "value_eur": 1000.0},
    ],
    "MATCH (d:Disclosure": [
        {"id": "d1", "title": "Apple orchard cohesion", "country": "PRT",
         "start_date": "2020-01-01", "fund": "ERDF", "nuts_code": "PT170",
         "description": "Planting apple orchards across the Norte region",
         "programme": "ERDF Norte", "company_gmr_id": "gmr-benef-1"},
    ],
    "MATCH (s:SanctionedEntity)": [
        {"id": "s1", "title": "Apple Sanctioned", "regime": "EU",
         "des_date": "2019-06-01", "rank": 0},
    ],
}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    cleanup_dishka()


def _client(rowmap=None):
    return make_test_client(neo4j_client=_Neo4j(rowmap if rowmap is not None else FULL_ROWMAP))


def test_returns_all_types_merged_and_ranked():
    r = _client().get("/search/results?q=apple")
    assert r.status_code == 200
    body = r.json()
    types_seen = {x["type"] for x in body["results"]}
    # every entity type contributes at least one result
    assert {"company", "authority", "person", "lobbyist", "contract",
            "cohesion", "sanction"} <= types_seen
    # highest score first — the exact company-name hit (rank 3) leads
    assert body["results"][0]["type"] == "company"
    assert body["results"][0]["title"] == "Apple Inc."
    assert body["counts"]["company"] == 2


def test_type_facet_restricts_results():
    r = _client().get("/search/results?q=apple&types=company,contract")
    body = r.json()
    assert set(body["types"]) == {"company", "contract"}
    assert {x["type"] for x in body["results"]} == {"company", "contract"}
    # unselected types are never queried
    assert "authority" not in body["counts"]


def test_date_filter_excludes_types_without_a_date():
    r = _client().get("/search/results?q=apple&date_from=2020-01-01")
    body = r.json()
    # only date-bearing types survive a date filter
    assert body["counts"]["company"] == 0
    assert body["counts"]["authority"] == 0
    assert body["counts"]["person"] == 0
    assert body["counts"]["contract"] == 1
    assert {x["type"] for x in body["results"]} <= {
        "lobbyist", "contract", "cohesion", "sanction"}


def test_geo_filter_excludes_types_without_geo():
    r = _client().get("/search/results?q=apple&country=PRT")
    body = r.json()
    # sanction + person have no geo dimension → excluded under a geo filter
    assert body["counts"]["sanction"] == 0
    assert body["counts"]["person"] == 0
    assert {x["type"] for x in body["results"]}.isdisjoint({"sanction", "person"})


def test_pagination_has_more_and_offset():
    r1 = _client().get("/search/results?q=apple&limit=2&offset=0")
    b1 = r1.json()
    assert len(b1["results"]) == 2
    assert b1["has_more"] is True
    r2 = _client().get("/search/results?q=apple&limit=2&offset=2")
    b2 = r2.json()
    # different slice
    assert b1["results"][0] != b2["results"][0]


def test_empty_query_rejected():
    assert _client().get("/search/results?q=").status_code == 422


def test_bad_date_format_rejected():
    assert _client().get("/search/results?q=x&date_from=2020").status_code == 422


def test_no_matches_returns_empty_page():
    r = _client({}).get("/search/results?q=zzz")
    body = r.json()
    assert body["results"] == []
    assert body["has_more"] is False


def test_results_carry_contextual_info():
    results = _client().get("/search/results?q=apple").json()["results"]
    apple = next(x for x in results if x["title"] == "Apple Inc.")
    # company context built from city + legal form
    assert apple["context"] == "Cupertino · Inc."
    # cohesion context from the project description
    cohesion = next(x for x in results if x["type"] == "cohesion")
    assert "apple orchards" in cohesion["context"].lower()
    # every result exposes a context field (empty allowed)
    assert all("context" in x for x in results)


# --- legislation (CELLAR mirror via Virtuoso) --------------------------------

from src.api.routers.search import (  # pylint: disable=wrong-import-position
    _celex_doc_type,
    _fallback_keywords,
    _ft_pattern,
    _legislation_query,
)


class _FakeVirtuoso:
    """Canned SPARQL bindings; records the query for assertions."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def query(self, q):
        self.queries.append(q)
        return self.rows


class _FakeLinguistics:
    def __init__(self, keywords):
        self._keywords = keywords

    def keywords(self, text, lang=None):  # pylint: disable=unused-argument
        return self._keywords


LEGISLATION_ROWS = [
    {"celex": "32024L1385", "date": "2024-05-14", "score": 42,
     "title_pref": "Directive (EU) 2024/1385 on combating violence against women",
     "title_any": "Directiva (UE) 2024/1385"},
    {"celex": "32011R0010", "date": "2011-01-14", "score": 17,
     "title_pref": None,
     "title_any": "Regolamento (UE) n. 10/2011"},
]


def test_celex_doc_type():
    assert _celex_doc_type("32024L1385") == "Directive"
    assert _celex_doc_type("32011R0010") == "Regulation"
    assert _celex_doc_type("72024L1385CZE_202501096") == "National implementing measure"
    assert _celex_doc_type("52022PC0105") == "Preparatory act"
    assert _celex_doc_type("") == "Legal document"


def test_fallback_keywords_keep_digits_drop_single_letters():
    assert _fallback_keywords("a Regulation 10/2011 x") == ["regulation", "10", "2011"]


def test_ft_pattern_quotes_and_joins():
    assert _ft_pattern(["violence", "women"]) == '"violence" AND "women"'
    # quote/backslash stripped, empty tokens dropped
    assert _ft_pattern(['vio"lence', "'", "women"]) == '"violence" AND "women"'


def test_legislation_query_date_filter_required_when_set():
    q = _legislation_query('"women"', "en", "2024-01-01", None, 10)
    assert 'STR(?dd) >= "2024-01-01"' in q
    assert "OPTIONAL { ?w cdm:work_date_document" not in q
    q2 = _legislation_query('"women"', "fr", None, None, 10)
    assert "OPTIONAL { ?w cdm:work_date_document" in q2
    assert "language/FRA" in q2


def test_search_results_includes_legislation():
    client = make_test_client(
        neo4j_client=_Neo4j(FULL_ROWMAP),
        virtuoso=_FakeVirtuoso(LEGISLATION_ROWS),
        linguistics=_FakeLinguistics(["violence", "women"]),
    )
    r = client.get("/search/results?q=violence against women&types=legislation")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["legislation"] == 2
    first = body["results"][0]
    assert first["type"] == "legislation"
    assert first["id"] == "32024L1385"
    assert first["title"].startswith("Directive (EU) 2024/1385")
    assert first["context"] == "Directive"
    assert first["meta"]["eurlex_url"].endswith("CELEX:32024L1385")
    # falls back to any-language title when preferred is missing
    second = body["results"][1]
    assert second["title"] == "Regolamento (UE) n. 10/2011"
    cleanup_dishka()


def test_legislation_without_virtuoso_returns_empty():
    client = make_test_client(neo4j_client=_Neo4j({}))
    r = client.get("/search/results?q=women&types=legislation")
    assert r.status_code == 200
    assert r.json()["counts"]["legislation"] == 0
    cleanup_dishka()


def test_legislation_uses_fallback_keywords_when_linguistics_down():
    fake_virt = _FakeVirtuoso(LEGISLATION_ROWS[:1])
    client = make_test_client(
        neo4j_client=_Neo4j({}),
        virtuoso=fake_virt,
    )
    r = client.get("/search/results?q=the violence against women&types=legislation")
    assert r.status_code == 200
    assert r.json()["counts"]["legislation"] == 1
    # naive fallback keeps "the" (len >= 2) — over-matching by design
    assert '"the" AND "violence"' in fake_virt.queries[0]
    cleanup_dishka()


def test_legislation_excluded_by_geo_filter():
    client = make_test_client(
        neo4j_client=_Neo4j(FULL_ROWMAP),
        virtuoso=_FakeVirtuoso(LEGISLATION_ROWS),
    )
    r = client.get("/search/results?q=women&types=legislation&country=FRA")
    assert r.status_code == 200
    assert r.json()["counts"]["legislation"] == 0
    cleanup_dishka()


def test_legislation_virtuoso_error_degrades_to_empty():
    class _Boom:
        def query(self, q):
            raise RuntimeError("store down")

    client = make_test_client(neo4j_client=_Neo4j({}), virtuoso=_Boom())
    r = client.get("/search/results?q=women&types=legislation")
    assert r.status_code == 200
    assert r.json()["counts"]["legislation"] == 0
    cleanup_dishka()


def test_cohesion_and_lobbyist_expose_link_targets():
    results = _client().get("/search/results?q=apple").json()["results"]
    cohesion = next(x for x in results if x["type"] == "cohesion")
    assert cohesion["meta"]["company_gmr_id"] == "gmr-benef-1"
    lobbyist = next(x for x in results if x["type"] == "lobbyist")
    assert lobbyist["meta"]["url"] == "www.apple-lobby.eu"
