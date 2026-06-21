"""Tests for the contracts API router."""
# The two `_run_side(*args, **kwargs)` factories pin the router's call-shape
# (it invokes `session.run(query, **params)`) without binding the values —
# the test asserts which scripted result comes back, not the query text.
# `_neo4j` is the canonical attribute the contracts source mounts; reaching
# into it to swap the session is how the existing mocks pin the wire shape.
# pylint: disable=protected-access,unused-argument
from unittest.mock import MagicMock

import httpx

from src.services import ted_lookup
from src.services.ted_lookup import TedLookupError
from tests.dishka_fixtures import make_test_client, cleanup_dishka


def _mock_contract_source(company_contracts=None, search_companies=None,
                          search_authorities=None):
    """Create a mock ContractDataSource with a mock Neo4j session."""
    source = MagicMock()
    source.get_company_contracts.return_value = company_contracts or {
        "gmr_id": "test-gid",
        "company_name": "Test Corp",
        "country": "DE",
        "total_contract_value_eur": 1000000,
        "contract_count": 2,
        "contracts": [
            {
                "ted_notice_id": "123-2024",
                "title": "IT Services",
                "value_eur": 500000,
                "award_date": "2024-06-15",
                "cpv": "72000000",
                "procedure_type": "open",
                "ted_url": "https://ted.europa.eu/en/notice/123-2024",
                "authority": "Ministry of X",
                "authority_id": "auth-ministry-x",
                "authority_country": "DE",
            },
            {
                "ted_notice_id": "456-2024",
                "title": "Consulting",
                "value_eur": 500000,
                "award_date": "2024-03-01",
                "cpv": "79000000",
                "procedure_type": "negotiated",
                "ted_url": "https://ted.europa.eu/en/notice/456-2024",
                "authority": "City of Y",
                "authority_id": "auth-city-y",
                "authority_country": "DE",
            },
        ],
    }
    source.get_contract_detail.return_value = {
        "ted_notice_id": "123-2024",
        "title": "IT Services",
        "value_eur": 500000,
    }
    source.get_authority_contracts.return_value = {
        "authority_id": "auth-id",
        "authority_name": "Ministry of X",
        "country": "DE",
        "total_spend_eur": 500000,
        "contract_count": 1,
        "contracts": [],
    }
    source.get_stored_publication_number.return_value = None
    source.get_sector_summary.return_value = [
        {"division": "72", "description": "IT services",
         "total_value": 500000, "contract_count": 1},
    ]

    # Mock the _neo4j.session() for unified search
    session = MagicMock()
    session.run.return_value.data.return_value = (
        search_companies or []
    )
    source._neo4j = MagicMock()
    source._neo4j.session.return_value.__enter__ = MagicMock(
        return_value=session
    )
    source._neo4j.session.return_value.__exit__ = MagicMock(
        return_value=False
    )
    # Second call (authorities) returns different data
    if search_authorities is not None:
        call_count = {"n": 0}

        def _run_side(*args, **kwargs):
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                result.data.return_value = search_companies or []
            else:
                result.data.return_value = search_authorities
            return result

        session.run = MagicMock(side_effect=_run_side)

    return source


class TestCompanyContracts:
    """Tests for GET /companies/{gmr_id}/contracts."""

    def test_returns_contracts(self):
        """Returns contract list for a known company."""
        mock = _mock_contract_source()
        client = make_test_client(contract_source=mock)
        resp = client.get("/companies/test-gid/contracts")
        cleanup_dishka()
        assert resp.status_code == 200
        data = resp.json()
        assert data["contract_count"] == 2
        assert data["company_name"] == "Test Corp"
        assert len(data["contracts"]) == 2
        assert data["contracts"][0]["ted_notice_id"] == "123-2024"

    def test_empty_for_unknown(self):
        """Returns empty list for unknown company."""
        mock = _mock_contract_source(company_contracts={
            "gmr_id": "unknown", "contracts": [], "contract_count": 0,
        })
        client = make_test_client(contract_source=mock)
        resp = client.get("/companies/unknown/contracts")
        cleanup_dishka()
        assert resp.status_code == 200
        assert resp.json()["contract_count"] == 0

    def test_each_row_carries_authority_id_for_profile_linking(self):
        """Every contract row must carry an `authority_id` so the
        contracts panel can link the authority cell back to its
        profile. Without this the panel could only render the
        authority as plain text.
        """
        mock = _mock_contract_source()
        client = make_test_client(contract_source=mock)
        resp = client.get("/companies/test-gid/contracts")
        cleanup_dishka()
        assert resp.status_code == 200
        contracts = resp.json()["contracts"]
        for row in contracts:
            assert "authority_id" in row, (
                f"authority_id missing from contract row {row.get('ted_notice_id')}"
            )
        assert contracts[0]["authority_id"] == "auth-ministry-x"
        assert contracts[1]["authority_id"] == "auth-city-y"


class TestContractDetail:
    """Tests for GET /contracts/{notice_id}."""

    def test_returns_detail(self):
        """Returns full contract detail."""
        mock = _mock_contract_source()
        client = make_test_client(contract_source=mock)
        resp = client.get("/contracts/123-2024")
        cleanup_dishka()
        assert resp.status_code == 200
        assert resp.json()["ted_notice_id"] == "123-2024"

    def test_404_for_unknown(self):
        """Returns 404 for unknown notice."""
        mock = _mock_contract_source()
        mock.get_contract_detail.return_value = None
        client = make_test_client(contract_source=mock)
        resp = client.get("/contracts/nonexistent")
        cleanup_dishka()
        assert resp.status_code == 404


class TestUnifiedSearch:
    """Tests for GET /search."""

    def test_returns_companies_and_authorities(self):
        """Search returns both entity types."""
        mock = _mock_contract_source()
        # Build a mock Neo4jClient whose session returns scripted query results
        neo4j_mock = MagicMock()
        session = MagicMock()
        call_count = {"n": 0}

        def _run_side(*args, **kwargs):
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                result.data.return_value = [
                    {"gmr_id": "gid-1", "name": "SOCOMEC", "country": "FR",
                     "ticker": None, "exchange": None, "currency": None,
                     "is_active": True},
                ]
            elif call_count["n"] == 2:
                result.data.return_value = []
            else:
                result.data.return_value = [
                    {"authority_id": "aid-1", "name": "DB Netz AG",
                     "country": "DE"},
                ]
            return result

        session.run = MagicMock(side_effect=_run_side)
        neo4j_mock.session.return_value.__enter__ = MagicMock(return_value=session)
        neo4j_mock.session.return_value.__exit__ = MagicMock(return_value=False)

        client = make_test_client(contract_source=mock, neo4j_client=neo4j_mock)
        resp = client.get("/search?q=test")
        cleanup_dishka()
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["companies"]) == 1
        assert data["companies"][0]["gmr_id"] == "gid-1"
        assert len(data["authorities"]) == 1
        assert data["authorities"][0]["authority_id"] == "aid-1"

    def test_empty_query_rejected(self):
        """Empty query returns 422."""
        mock = _mock_contract_source()
        client = make_test_client(contract_source=mock)
        resp = client.get("/search?q=")
        cleanup_dishka()
        assert resp.status_code == 422

    def test_listed_query_orders_results_by_rank(self):
        """Smoke SEARCH-04 caught that an exact-name match could surface
        AFTER a CONTAINS-only hit (e.g. searching for "Apple" returned
        PINEAPPL.L before AAPL because Neo4j had no implicit ordering and
        both companies' names matched via CONTAINS). Pin the rank tiers
        and the ORDER BY here so a regression on this query trips a unit
        test instead of a smoke retry.
        """
        # The Cypher must contain the CASE-when rank ladder and an
        # ORDER BY rank DESC before LIMIT — that's the load-bearing fix.
        # We assert the query shape rather than rerunning a real graph.
        from src.api.routers import contracts as contracts_router  # pylint: disable=import-outside-toplevel
        import inspect  # pylint: disable=import-outside-toplevel
        source = inspect.getsource(contracts_router.unified_search)
        assert "CASE" in source
        assert "STARTS WITH toLower($q)" in source
        assert "ORDER BY rank DESC" in source

    def test_rank_field_not_leaked_to_client(self):
        """`rank` is internal to the Cypher; clients see ordered rows."""
        mock = _mock_contract_source()
        neo4j_mock = MagicMock()
        session = MagicMock()
        call_count = {"n": 0}

        def _run_side(*_args, **_kwargs):
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                # Listed companies — Neo4j returns rank=3 for an exact
                # name match. The response must strip it.
                result.data.return_value = [
                    {"gmr_id": "g-1", "name": "Apple Inc.", "country": "USA",
                     "ticker": "AAPL", "exchange": "US", "currency": "USD",
                     "is_active": True, "rank": 3},
                ]
            else:
                result.data.return_value = []
            return result

        session.run = MagicMock(side_effect=_run_side)
        neo4j_mock.session.return_value.__enter__ = MagicMock(return_value=session)
        neo4j_mock.session.return_value.__exit__ = MagicMock(return_value=False)

        client = make_test_client(contract_source=mock, neo4j_client=neo4j_mock)
        resp = client.get("/search?q=Apple")
        cleanup_dishka()
        assert resp.status_code == 200
        company = resp.json()["companies"][0]
        assert company["ticker"] == "AAPL"
        assert "rank" not in company


class TestSectorSummary:
    """Tests for GET /contracts/sectors."""

    def test_returns_sectors(self):
        """Returns CPV sector aggregation."""
        mock = _mock_contract_source()
        client = make_test_client(contract_source=mock)
        resp = client.get("/contracts/sectors")
        cleanup_dishka()
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["division"] == "72"


class TestTedLink:
    """Tests for GET /contracts/{notice_id}/ted-link — the 302
    redirector that translates our eForms UUID to TED's canonical
    publication-number URL."""

    def test_redirects_302_to_canonical_ted_url(self, monkeypatch):
        """Happy path: TED resolves the UUID → publication-number, the
        endpoint 302s to https://ted.europa.eu/en/notice/-/detail/<pub>.
        Pin both the status and Location header so a regression in
        either is loud."""
        ted_lookup.resolve_publication_number.cache_clear()
        monkeypatch.setattr(
            "src.api.routers.contracts.resolve_publication_number",
            lambda _uuid: "295342-2026",
        )
        mock = _mock_contract_source()
        client = make_test_client(contract_source=mock)
        resp = client.get(
            "/contracts/912f1717-1ace-413d-aa61-cd21cd6b95e7/ted-link",
            follow_redirects=False,
        )
        cleanup_dishka()
        assert resp.status_code == 302
        assert resp.headers["location"] == \
            "https://ted.europa.eu/en/notice/-/detail/295342-2026"

    def test_returns_404_when_ted_has_no_match(self, monkeypatch):
        """TedLookupError → 404 with the lookup message verbatim. Pre-
        publication notices and bad UUIDs both hit this path."""

        def _raise(_uuid):
            raise TedLookupError("TED has no published notice for X")
        monkeypatch.setattr(
            "src.api.routers.contracts.resolve_publication_number", _raise,
        )
        mock = _mock_contract_source()
        client = make_test_client(contract_source=mock)
        resp = client.get(
            "/contracts/X/ted-link", follow_redirects=False,
        )
        cleanup_dishka()
        assert resp.status_code == 404
        assert "no published notice" in resp.json()["detail"]

    def test_returns_502_when_ted_api_errors(self, monkeypatch):
        """httpx transport errors (TED outage, DNS, timeout) bubble up
        to the router which maps them to 502 so downstream callers
        can distinguish "TED is down" from "no such notice"."""

        def _raise(_uuid):
            raise httpx.ConnectError("backend down")
        monkeypatch.setattr(
            "src.api.routers.contracts.resolve_publication_number", _raise,
        )
        mock = _mock_contract_source()
        client = make_test_client(contract_source=mock)
        resp = client.get(
            "/contracts/X/ted-link", follow_redirects=False,
        )
        cleanup_dishka()
        assert resp.status_code == 502
        assert "TED search API error" in resp.json()["detail"]


    def test_uses_stored_pub_num_and_skips_live_lookup(self, monkeypatch):
        """When the Contract row already carries a publication-number
        (ETL captured it at ingest time, or backfill landed it),
        the router 302s directly with the stored value and never
        hits TED's search API. Critical for cold-pod performance and
        to keep TED out of the click-path."""
        ted_lookup.resolve_publication_number.cache_clear()
        live_calls = {"n": 0}

        def _live_should_not_run(_uuid):
            live_calls["n"] += 1
            return "live-99999-2026"
        monkeypatch.setattr(
            "src.api.routers.contracts.resolve_publication_number",
            _live_should_not_run,
        )
        mock = _mock_contract_source()
        mock.get_stored_publication_number.return_value = "295342-2026"
        client = make_test_client(contract_source=mock)
        resp = client.get(
            "/contracts/912f1717-1ace-413d-aa61-cd21cd6b95e7/ted-link",
            follow_redirects=False,
        )
        cleanup_dishka()
        assert resp.status_code == 302
        assert resp.headers["location"] == \
            "https://ted.europa.eu/en/notice/-/detail/295342-2026"
        assert live_calls["n"] == 0, (
            "stored pub-num must fully short-circuit the TED v3 search "
            "call — found "
            f"{live_calls['n']} live lookup(s)"
        )

    def test_falls_through_to_live_when_stored_is_empty_string(
        self, monkeypatch,
    ):
        """An empty string is treated the same as None — fall through
        to the live lookup. Prevents a backfill or ETL bug that
        wrote '' instead of leaving the property unset from
        silently breaking the redirect path."""
        ted_lookup.resolve_publication_number.cache_clear()
        monkeypatch.setattr(
            "src.api.routers.contracts.resolve_publication_number",
            lambda _uuid: "295342-2026",
        )
        mock = _mock_contract_source()
        # Defense in depth: even though get_stored_publication_number
        # in the GraphContractSource normalises '' → None, exercise
        # the router's own guard too.
        mock.get_stored_publication_number.return_value = ""
        client = make_test_client(contract_source=mock)
        resp = client.get(
            "/contracts/X/ted-link", follow_redirects=False,
        )
        cleanup_dishka()
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/295342-2026")


class TestSingleBidderEngine:
    """Tests for the SMSB single-bidder-rate endpoints + source methods."""

    def test_single_bidder_rate_endpoint(self):
        """The scoped single-bidder-rate endpoint returns the source result."""
        mock = _mock_contract_source()
        mock.get_single_bidder_stats.return_value = {
            "scope": {"country": "HUN", "cpv": None},
            "total": 100, "single_bidder": 40, "single_bidder_rate": 0.4,
        }
        client = make_test_client(contract_source=mock)
        resp = client.get("/contracts/single-bidder-rate?country=HUN")
        cleanup_dishka()
        assert resp.status_code == 200
        assert resp.json()["single_bidder_rate"] == 0.4
        mock.get_single_bidder_stats.assert_called_once_with(country="HUN", cpv=None)

    def test_single_bidder_by_country_endpoint(self):
        """The per-country benchmark endpoint returns the source result."""
        mock = _mock_contract_source()
        mock.get_single_bidder_by_country.return_value = [
            {"country": "HUN", "total": 200, "single": 90,
             "single_bidder_rate": 0.45},
        ]
        client = make_test_client(contract_source=mock)
        resp = client.get("/contracts/single-bidder-by-country")
        cleanup_dishka()
        assert resp.status_code == 200
        assert resp.json()[0]["country"] == "HUN"

    def test_integrity_block_derives_flags(self):
        """The detail integrity block carries fields + derived red flags."""
        from src.data.graph.graph_contract_source import (  # pylint: disable=import-outside-toplevel
            GraphContractSource)
        src = GraphContractSource(MagicMock())
        block = src._integrity_block({  # pylint: disable=protected-access
            "tenders_received": 1, "procedure_type": "neg-wo-call",
            "award_criterion_type": "price"})
        assert block["tenders_received"] == 1
        assert block["is_single_bidder"] is True
        assert block["is_no_call"] is True
        assert block["integrity_red_flags"] == 4

    def test_get_single_bidder_stats_computes_rate(self):
        """The source computes the single-bidder rate from the count query."""
        from src.data.graph.graph_contract_source import (  # pylint: disable=import-outside-toplevel
            GraphContractSource)
        src = GraphContractSource(MagicMock())
        session = MagicMock()
        session.run.return_value.single.return_value = {"total": 100, "single": 40}
        src._neo4j.session.return_value.__enter__ = MagicMock(  # pylint: disable=protected-access
            return_value=session)
        src._neo4j.session.return_value.__exit__ = MagicMock(  # pylint: disable=protected-access
            return_value=False)
        out = src.get_single_bidder_stats(country="HUN")
        assert out["total"] == 100 and out["single_bidder_rate"] == 0.4


def test_get_single_bidder_by_country_source():
    """The per-country benchmark source method returns the query rows."""
    from src.data.graph.graph_contract_source import (  # pylint: disable=import-outside-toplevel
        GraphContractSource)
    src = GraphContractSource(MagicMock())
    session = MagicMock()
    session.run.return_value.data.return_value = [
        {"country": "HUN", "total": 200, "single": 90,
         "single_bidder_rate": 0.45},
    ]
    src._neo4j.session.return_value.__enter__ = MagicMock(  # pylint: disable=protected-access
        return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(  # pylint: disable=protected-access
        return_value=False)
    rows = src.get_single_bidder_by_country(min_sample=50, limit=10)
    assert rows[0]["country"] == "HUN" and rows[0]["single_bidder_rate"] == 0.45


def test_get_contract_detail_includes_integrity():
    """get_contract_detail returns the integrity block with derived flags."""
    from src.data.graph.graph_contract_source import (  # pylint: disable=import-outside-toplevel
        GraphContractSource)
    src = GraphContractSource(MagicMock())
    session = MagicMock()
    ct = {"ted_notice_id": "n1", "title": "Books", "value_eur": 5000,
          "cpv": "72000000", "procedure_type": "neg-wo-call",
          "publication_date": "2026-01-15", "tenders_received": 1,
          "award_criterion_type": "price"}
    auth = {"name": "Ministry", "country": "HUN"}
    comp = {"gmr_id": "g1", "name": "Acme", "country": "HUN"}
    session.run.return_value.single.return_value = {
        "ct": ct, "a": auth, "c": comp, "cpv": None}
    src._neo4j.session.return_value.__enter__ = MagicMock(  # pylint: disable=protected-access
        return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(  # pylint: disable=protected-access
        return_value=False)
    out = src.get_contract_detail("n1")
    assert out["procedure_type"] == "neg-wo-call"
    integ = out["integrity"]
    assert integ["is_single_bidder"] is True
    assert integ["is_no_call"] is True
    assert integ["integrity_red_flags"] >= 2


def test_get_contract_detail_missing_returns_none():
    """A missing contract row yields None (404 path)."""
    from src.data.graph.graph_contract_source import (  # pylint: disable=import-outside-toplevel
        GraphContractSource)
    src = GraphContractSource(MagicMock())
    session = MagicMock()
    session.run.return_value.single.return_value = None
    src._neo4j.session.return_value.__enter__ = MagicMock(  # pylint: disable=protected-access
        return_value=session)
    src._neo4j.session.return_value.__exit__ = MagicMock(  # pylint: disable=protected-access
        return_value=False)
    assert src.get_contract_detail("nope") is None
