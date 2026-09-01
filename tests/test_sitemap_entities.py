"""Entity sitemap shards: listed companies and top authorities.

The shape under test is the per-country sharding. A single global "top N"
list buries small member states — the whole of Malta sits below the
German 500th — so every country gets its own file and its own budget.
"""
# pylint: disable=missing-class-docstring,missing-function-docstring,unused-argument,too-few-public-methods,protected-access
from __future__ import annotations

from tests.dishka_fixtures import make_test_client, cleanup_dishka

from src.api.routers import sitemap_entities as se


class _Tx:
    def __init__(self, rows, spy):
        self._rows, self._spy = rows, spy

    def run(self, query, parameters=None, **kwargs):
        self._spy["query"] = query
        self._spy["parameters"] = parameters
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Session:
    def __init__(self, rows, spy):
        self._rows, self._spy = rows, spy

    def begin_transaction(self, **config):
        self._spy["tx_config"] = config
        return _Tx(self._rows, self._spy)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Neo4j:
    def __init__(self, rows, spy):
        self._rows, self._spy = rows, spy

    def session(self, **config):
        self._spy["session_config"] = config
        return _Session(self._rows, self._spy)

    def close(self):
        pass


def _client(ids, spy=None):
    # `spy if spy is not None`, not `spy or {}`: an empty dict is falsy,
    # so `or` would hand the fake a different dict than the test holds
    # and every assertion on it would KeyError.
    return make_test_client(
        neo4j_client=_Neo4j([{"id": i} for i in ids], {} if spy is None else spy))


def test_companies_shard_lists_one_url_per_company():
    c = _client(["a1", "b2"])
    try:
        r = c.get("/sitemap-companies-DEU.xml")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/xml")
        assert "<loc>https://fontem.eu/company/a1</loc>" in r.text
        assert "<loc>https://fontem.eu/company/b2</loc>" in r.text
        assert r.text.count("<url>") == 2
    finally:
        cleanup_dishka()


def test_authorities_shard_is_capped_per_country_not_globally():
    """500 for every country, so a small member state is not squeezed out
    by a larger one's tail."""
    spy = {}
    c = _client(["x"], spy)
    try:
        assert c.get("/sitemap-authorities-MLT.xml").status_code == 200
        assert spy["parameters"]["limit"] == se.AUTHORITIES_PER_COUNTRY
        assert spy["parameters"]["country"] == "MLT"
    finally:
        cleanup_dishka()


def test_every_listed_country_gets_its_own_shard():
    c = _client(["x"])
    try:
        for code in ("MLT", "CYP", "DEU", "PRT"):
            assert c.get(f"/sitemap-authorities-{code}.xml").status_code == 200
    finally:
        cleanup_dishka()


def test_a_country_code_is_normalised_to_upper_case():
    spy = {}
    c = _client(["x"], spy)
    try:
        assert c.get("/sitemap-companies-deu.xml").status_code == 200
        assert spy["parameters"]["country"] == "DEU"
    finally:
        cleanup_dishka()


def test_an_unknown_country_is_404_not_an_empty_file():
    """An empty urlset would hide a broken link in the index."""
    c = _client([])
    try:
        assert c.get("/sitemap-companies-ZZZ.xml").status_code == 404
        assert c.get("/sitemap-authorities-USA.xml").status_code == 404
    finally:
        cleanup_dishka()


def test_the_country_list_is_alpha_3_only():
    """Alpha-2 anywhere is the drift #405 fixed at source; a two-letter
    code here would silently match nothing in the graph."""
    assert se.COUNTRIES
    assert all(len(c) == 3 and c.isupper() for c in se.COUNTRIES)
    for expected in ("DEU", "FRA", "PRT", "MLT", "CYP"):
        assert expected in se.COUNTRIES


def test_queries_filter_to_current_valued_contracts_and_active_listings():
    """Superseded notices would double-count spend, and a delisted shell
    is not something to invite a crawler to index."""
    assert "c.is_current" in se._TOP_AUTHORITIES
    assert "c.value_eur IS NOT NULL" in se._TOP_AUTHORITIES
    assert "l.active" in se._LISTED_COMPANIES


def test_the_read_runs_in_a_bounded_read_transaction():
    spy = {}
    c = _client(["x"], spy)
    try:
        c.get("/sitemap-companies-DEU.xml")
        assert spy["tx_config"]["timeout"] == se._QUERY_TIMEOUT_S
    finally:
        cleanup_dishka()
