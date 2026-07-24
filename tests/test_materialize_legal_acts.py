"""Tests for the CELEX spine materializer + petition linker (P1/P2)."""
from __future__ import annotations

from src.etl.legislative.materialize_legal_acts import (
    ANSWER_CLASS_RE,
    doc_type,
    link_petitions,
    match_answers,
    materialize_spine,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows

    def single(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, store):
        self.store = store

    def run(self, query, **params):
        self.store.append((query, params))
        if "MATCH (p:Petition)" in query:
            return _FakeResult(self.store_petitions)
        return _FakeResult([])

    store_petitions: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeDriver:
    def __init__(self, petitions=None):
        self.calls: list = []
        _FakeSession.store_petitions = petitions or []

    def session(self):
        return _FakeSession(self.calls)


def test_doc_type():
    assert doc_type("32024L1385") == "Directive"
    assert doc_type("32024D1824") == "Decision"
    assert doc_type("52026DC4110") == "Preparatory act"


# Real prod pairs from the 2026-07-24 validation run: register title,
# answered date, and the CELLAR communication that answers it.
_REAL_PAIRS = [
    ("Water and sanitation are a human right! Water is a public good, "
     "not a commodity!", "2014-03-19", "52014DC0177",
     "COMMUNICATION FROM THE COMMISSION on the European Citizens' "
     "Initiative \"Water and sanitation are a human right! Water is a "
     "public good, not a commodity!\"", "2014-03-19"),
    ("One of us", "2014-05-28", "52014DC0355",
     "COMMUNICATION FROM THE COMMISSION on the European Citizens' "
     "Initiative \"One of us\"", "2014-05-28"),
    ("End the Cage Age", "2021-06-30", "52021XC0709(01)",
     "Communication from the Commission on the European Citizens\u2019 "
     "Initiative (ECI) \u2018End the Cage Age\u2019", "2021-07-09"),
    ("Fur Free Europe", "2023-12-07", "52023XC01559",
     "Communication from the Commission on the European Citizens\u2019 "
     "Initiative (ECI) Fur Free Europe", "2023-12-21"),
    ("Stop Destroying Videogames", "2026-06-16", "52026XC03601",
     "Communication from the commission on the European Citizens\u2019 "
     "Initiative (ECI) Stop Destroying Videogames", "2026-07-09"),
]


def _cands():
    return {cx: {"title": ct, "date": cd}
            for _, _, cx, ct, cd in _REAL_PAIRS}


def _pets():
    return [{"pid": f"P{i}", "title": t, "answered_date": ad,
             "status": "ANSWERED"}
            for i, (t, ad, _, _, _) in enumerate(_REAL_PAIRS)]


def test_match_answers_links_real_pairs_uniquely():
    got = match_answers(_pets(), _cands())
    assert len(got) == len(_REAL_PAIRS)
    for i, (_, _, cx, _, _) in enumerate(_REAL_PAIRS):
        celex, tier, delta = got[f"P{i}"]
        assert celex == cx
        assert tier == "title-substring"
        assert delta <= 45


def test_match_answers_refuses_near_miss_title():
    # Real near-miss: register says "cultures", the communication says
    # "culture" — one letter of fuzz must yield NO link, not a guess.
    pets = [{"pid": "P0", "status": "ANSWERED", "answered_date": "2025-09-03",
             "title": "Cohesion policy for the equality of the regions and "
                      "sustainability of the regional cultures"}]
    cands = {"52025XC04991": {
        "title": "Communication from the commission on the European "
                 "Citizens\u2019 Initiative Cohesion policy for the equality "
                 "of the regions and sustainability of the regional culture",
        "date": "2025-09-11"}}
    assert match_answers(pets, cands) == {}


def test_match_answers_rejects_date_disagreement():
    pairs = [(_REAL_PAIRS[1][0], "2019-01-01") + _REAL_PAIRS[1][2:]]
    pets = [{"pid": "P0", "title": pairs[0][0], "answered_date": pairs[0][1],
             "status": "ANSWERED"}]
    assert match_answers(pets, _cands()) == {}


def test_match_answers_enforces_injectivity():
    # Two petitions whose titles both sit inside one candidate title
    # must both drop rather than share the same answer document.
    pets = [
        {"pid": "A", "title": "One of us", "answered_date": "2014-05-28",
         "status": "ANSWERED"},
        {"pid": "B", "title": "of us", "answered_date": "2014-05-28",
         "status": "ANSWERED"},
    ]
    assert match_answers(pets, {_REAL_PAIRS[1][2]: {
        "title": _REAL_PAIRS[1][3], "date": _REAL_PAIRS[1][4]}}) == {}


def test_answer_class_regexp_bounds_the_search():
    assert ANSWER_CLASS_RE.match(_REAL_PAIRS[3][3])
    assert not ANSWER_CLASS_RE.match(
        "Opinion of the European Economic and Social Committee on the "
        "Communication from the Commission on the European Citizens' "
        "Initiative Water")


def test_spine_pages_until_short_page(monkeypatch):
    responses = [
        # page 1: keys, then VALUES-bound details
        [{"celex": "32024D0001"}, {"celex": "32024D0002"}],
        [{"celex": "32024D0001", "date": "2024-01-01"},
         {"celex": "32024D0002"}],
        # page 2: no more keys
        [],
    ]
    calls = []

    def fake_sparql(_endpoint, query):
        calls.append(query)
        return responses.pop(0)

    monkeypatch.setattr(
        "src.etl.legislative.materialize_legal_acts.sparql", fake_sparql)
    monkeypatch.setattr(
        "src.etl.legislative.materialize_legal_acts.PAGE", 2)
    driver = _FakeDriver()
    n = materialize_spine("http://x", driver)
    assert n == 2
    # keys page is join-free; details are VALUES-bound to that page
    assert "SELECT DISTINCT ?celex" in calls[0]
    assert 'VALUES ?cx { "32024D0001"^^xsd:string "32024D0002"^^xsd:string }' in calls[1]
    # keyset: the next keys query filters past the last celex of page one
    assert '> "32024D0002"' in calls[2]
    assert any("MERGE (a:LegalAct" in q for q, _ in driver.calls)


def test_link_petitions_registration_only_when_not_answered(monkeypatch):
    # A registered-but-unanswered petition gets its REGISTERED_BY edge and
    # the answer machinery never fires (fail-closed on status).
    petitions = [{
        "system": "eu-eci", "pid": "ECI(2024)000007", "status": "ONGOING",
        "title": "Stop Destroying Videogames", "answered_date": None,
        "reg": "32024D1824", "refs": [],
    }]

    def fake_sparql(_endpoint, query):
        assert "work_id_document" not in query  # T0 must not run
        return [{"celex": "32024D1824", "date": "2024-06-17",
                 "title": "Commission Decision ..."}]

    monkeypatch.setattr(
        "src.etl.legislative.materialize_legal_acts.sparql", fake_sparql)
    driver = _FakeDriver(petitions)
    stats = link_petitions("http://x", driver)
    assert stats["resolved"] == 1
    assert stats["edges"] == 1
    assert stats["unresolved"] == 0
    joined = " ".join(q for q, _ in driver.calls)
    assert "REGISTERED_BY" in joined
    assert "ANSWERED_BY" not in joined


def test_link_petitions_answered_via_ref_exact(monkeypatch):
    # T0: the register C-number joins cdm:work_id_document exactly; the
    # linked CELEX is whatever the mirror says (never ref arithmetic).
    petitions = [{
        "system": "eu-eci", "pid": "ECI(2022)000002", "status": "ANSWERED",
        "title": "Fur Free Europe", "answered_date": "2023-12-07",
        "reg": None, "refs": ["C(2023)8362"],
    }]

    def fake_sparql(_endpoint, query):
        if "work_id_document" in query:
            assert "immc:C(2023)8362/" in query
            return [{"celex": "52023XC01559", "date": "2023-12-21",
                     "title": "Communication from the Commission on the "
                              "European Citizens\u2019 Initiative (ECI) "
                              "Fur Free Europe"}]
        return []

    monkeypatch.setattr(
        "src.etl.legislative.materialize_legal_acts.sparql", fake_sparql)
    driver = _FakeDriver(petitions)
    stats = link_petitions("http://x", driver)
    assert stats["edges"] == 1
    joined = " ".join(q for q, _ in driver.calls)
    assert "ANSWERED_BY" in joined
    params = [p for _, p in driver.calls if p.get("matched")]
    assert params[0]["matched"] == "ref-exact"
    assert params[0]["celex"] == "52023XC01559"
