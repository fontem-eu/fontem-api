"""Tests for the petitions API (list + detail with linked legislation)."""
from tests.dishka_fixtures import cleanup_dishka, make_test_client


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows


class _Session:
    def __init__(self, rowmap):
        self._rowmap = rowmap

    def run(self, query, **_params):
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


LIST_ROWMAP = {
    "count(*) AS n": [
        {"status": "ANSWERED", "n": 14}, {"status": "REGISTERED", "n": 40},
        {"status": None, "n": 1},
    ],
    "ORDER BY coalesce(p.total_supporters, 0) DESC": [
        {"system": "eu-eci", "petition_id": "ECI(2024)000007",
         "title": "Stop Destroying Videogames", "status": "ANSWERED",
         "total_supporters": 1294188, "registration_date": "2024-06-19",
         "answered_date": "2026-06-16", "latest_update": "2026-06-16"},
    ],
}

DETAIL_ROWMAP = {
    "OPTIONAL MATCH (p)-[r:REGISTERED_BY|ANSWERED_BY|LED_TO]": [{
        "petition": {
            "system": "eu-eci", "petition_id": "ECI(2024)000007",
            "title": "Stop Destroying Videogames", "status": "ANSWERED",
            "total_supporters": 1294188,
            "answer_refs": ["C(2026)4110"],
        },
        "acts": [
            {"rel": "REGISTERED_BY", "celex": "32024D1824",
             "title_en": "Commission Implementing Decision ...",
             "title_fr": None, "date": "2024-06-17", "doc_type": "Decision"},
            {"rel": None, "celex": None, "title_en": None,
             "title_fr": None, "date": None, "doc_type": None},
        ],
    }],
}


def test_list_with_counts_and_filter():
    client = make_test_client(neo4j_client=_Neo4j(LIST_ROWMAP))
    r = client.get("/petitions?status=ANSWERED")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"] == {"ANSWERED": 14, "REGISTERED": 40}
    assert body["total"] == 54
    assert body["results"][0]["petition_id"] == "ECI(2024)000007"
    cleanup_dishka()


def test_detail_links_and_unresolved_refs():
    client = make_test_client(neo4j_client=_Neo4j(DETAIL_ROWMAP))
    r = client.get("/petitions/detail?petition_id=ECI(2024)000007")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["petition"]["total_supporters"] == 1294188
    assert len(body["legislation"]) == 1
    act = body["legislation"][0]
    assert act["rel"] == "REGISTERED_BY"
    assert act["eurlex_url"].endswith("CELEX:32024D1824")
    # the answer doc is documented but not linkable yet — surfaced honestly
    assert body["unresolved_answer_refs"] == ["C(2026)4110"]
    cleanup_dishka()


def test_detail_404():
    client = make_test_client(neo4j_client=_Neo4j({}))
    r = client.get("/petitions/detail?petition_id=ECI(1999)000001")
    assert r.status_code == 404
    cleanup_dishka()
