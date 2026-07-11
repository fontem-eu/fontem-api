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
        {"id": "c1", "title": "Apple Inc.", "country": "USA", "ticker": "AAPL", "rank": 3},
        {"id": "c2", "title": "Pineapple Power", "country": "GBR", "ticker": None, "rank": 0},
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
         "category": "In-house", "reg_date": "2021-03-01"},
    ],
    "MATCH (ct:Contract)": [
        {"id": "t1", "title": "Apple procurement", "country": "PRT",
         "pub_date": "2022-05-01", "value_eur": 1000.0},
    ],
    "MATCH (d:Disclosure": [
        {"id": "d1", "title": "Apple orchard cohesion", "country": "PRT",
         "start_date": "2020-01-01", "fund": "ERDF", "nuts_code": "PT170"},
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
