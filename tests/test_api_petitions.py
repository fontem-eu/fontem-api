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
        {"p": {"system": "eu-eci", "petition_id": "ECI(2024)000007",
               "title": "Stop Destroying Videogames", "status": "ANSWERED",
               "total_supporters": 1294188, "registration_date": "2024-06-19",
               "answered_date": "2026-06-16", "latest_update": "2026-06-16"}},
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
            {"rel": "REGISTERED_BY", "act": {
                "celex": "32024D1824", "title_en": "Commission Implementing Decision ...",
                "date_document": "2024-06-17", "doc_type": "Decision"}},
            {"rel": None, "act": None},
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


# --- petitions showcase: multi-status filter + sort variants ---------------

MULTI_ROWMAP = {
    "count(*) AS n": [
        {"status": "ANSWERED", "n": 14}, {"status": "SUBMITTED", "n": 3},
    ],
    "WHERE p.status IN $statuses": [
        {"p": {"system": "eu-eci", "petition_id": "ECI(2024)000007",
         "title": "Stop Destroying Videogames", "status": "ANSWERED",
         "total_supporters": 1294188, "registration_date": "2024-06-19",
         "answered_date": "2026-06-16", "latest_update": "2026-06-16"}},
    ],
}

RECENT_ROWMAP = {
    "count(*) AS n": [{"status": "SUBMITTED", "n": 2}],
    "ORDER BY coalesce(p.registration_date, '') DESC": [
        {"p": {"system": "eu-eci", "petition_id": "ECI(2025)000010",
         "title": "Newer", "status": "SUBMITTED",
         "total_supporters": 1000000, "registration_date": "2025-05-01",
         "answered_date": None, "latest_update": "2025-05-02"}},
        {"p": {"system": "eu-eci", "petition_id": "ECI(2024)000007",
         "title": "Older", "status": "ANSWERED",
         "total_supporters": 1294188, "registration_date": "2024-06-19",
         "answered_date": "2026-06-16", "latest_update": "2026-06-16"}},
    ],
}


def test_list_multi_status_filter():
    client = make_test_client(neo4j_client=_Neo4j(MULTI_ROWMAP))
    r = client.get("/petitions?statuses=SUBMITTED,VERIFICATION,ANSWERED")
    assert r.status_code == 200, r.text
    body = r.json()
    # Rows only come back when the query built the `p.status IN $statuses`
    # filter — the stub answers to that anchor alone, proving the
    # multi-status branch (which takes precedence over single `status`).
    assert [row["petition_id"] for row in body["results"]] == ["ECI(2024)000007"]
    assert body["total"] == 17
    cleanup_dishka()


def test_list_sort_recent_uses_recent_order():
    client = make_test_client(neo4j_client=_Neo4j(RECENT_ROWMAP))
    r = client.get("/petitions?sort=recent")
    assert r.status_code == 200, r.text
    ids = [row["petition_id"] for row in r.json()["results"]]
    # A non-empty result proves the `recent` ORDER BY clause was built: the
    # rowmap only answers to that anchor. The stub returns them already
    # most-recent-registered first.
    assert ids == ["ECI(2025)000010", "ECI(2024)000007"]
    cleanup_dishka()


def test_list_defaults_supporters_order_unchanged():
    client = make_test_client(neo4j_client=_Neo4j(LIST_ROWMAP))
    r = client.get("/petitions")
    assert r.status_code == 200, r.text
    body = r.json()
    # No `statuses`, default `sort=supporters`: still hits the original
    # supporters-ordered query and the unchanged counts/total envelope.
    assert body["results"][0]["petition_id"] == "ECI(2024)000007"
    assert body["counts"] == {"ANSWERED": 14, "REGISTERED": 40}
    assert body["total"] == 54
    cleanup_dishka()


OBJECTIVES = "Require publishers that sell videogames to leave them playable."
VIDEOGAMES = {
    "system": "eu-eci", "petition_id": "ECI(2024)000007", "status": "ANSWERED",
    "title": "Stop Destroying Videogames", "title_lang": "en", "objectives": OBJECTIVES,
    "title_en": "Stop Destroying Videogames", "objectives_en": OBJECTIVES,
    "title_fr": "Stop à la destruction des jeux vidéo",
    "objectives_fr": "Obliger les éditeurs à laisser les jeux vidéo jouables.",
    "title_de": "Stoppt die Zerstörung von Videospielen",
    "objectives_summarized_from": OBJECTIVES,
    "objectives_summary_en": "Asks the EU to keep sold games playable.",
    "objectives_summary_fr": "Demande à l'UE de garder jouables les jeux vendus.",
    "total_supporters": 1294188,
}


def _client(node, anchor="OPTIONAL MATCH (p)-[r:REGISTERED_BY|ANSWERED_BY|LED_TO]"):
    rows = [{"petition": node, "acts": []}] if "OPTIONAL" in anchor else [{"p": node}]
    return make_test_client(neo4j_client=_Neo4j({anchor: rows, "count(*) AS n": []}))


def test_a_reader_gets_the_official_version_in_their_language_and_its_summary():
    client = _client(VIDEOGAMES)
    try:
        p = client.get("/petitions/detail?petition_id=ECI(2024)000007&lang=fr").json()["petition"]
        assert p["title"] == "Stop à la destruction des jeux vidéo"
        assert p["objectives"] == "Obliger les éditeurs à laisser les jeux vidéo jouables."
        assert p["title_original"] == "Stop Destroying Videogames"
        assert p["summary"] == "Demande à l'UE de garder jouables les jeux vendus."
        assert p["language_shown"] == "fr" and p["languages"] == ["de", "en", "fr"]
        assert "objectives_fr" not in p and "objectives_summary_en" not in p
    finally:
        cleanup_dishka()


def test_a_language_without_an_official_version_falls_back_to_english():
    client = _client(VIDEOGAMES)
    try:
        p = client.get("/petitions/detail?petition_id=ECI(2024)000007&lang=pl").json()["petition"]
        assert p["title"] == "Stop Destroying Videogames" and p["language_shown"] == "en"
        assert p["summary"] == "Asks the EU to keep sold games playable."
        de = client.get("/petitions/detail?petition_id=ECI(2024)000007&lang=de").json()["petition"]
        assert de["title"].startswith("Stoppt") and de["objectives"] == OBJECTIVES
    finally:
        cleanup_dishka()


def test_a_summary_of_objectives_since_changed_is_not_shown():
    changed = {**VIDEOGAMES, "objectives": "The objectives as they read now."}
    client = _client(changed)
    try:
        p = client.get("/petitions/detail?petition_id=ECI(2024)000007&lang=fr").json()["petition"]
        assert p["summary"] is None
    finally:
        cleanup_dishka()


def test_the_list_shows_titles_and_summaries_in_the_readers_language():
    client = _client(VIDEOGAMES, anchor="ORDER BY coalesce(p.total_supporters, 0) DESC")
    try:
        (row,) = client.get("/petitions?lang=fr").json()["results"]
        assert row["title"] == "Stop à la destruction des jeux vidéo"
        assert row["summary"] == "Demande à l'UE de garder jouables les jeux vendus."
        assert row["total_supporters"] == 1294188 and "objectives" not in row
    finally:
        cleanup_dishka()
