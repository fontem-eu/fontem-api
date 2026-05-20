"""Tests for the contracts API router."""
# The two `_run_side(*args, **kwargs)` factories pin the router's call-shape
# (it invokes `session.run(query, **params)`) without binding the values —
# the test asserts which scripted result comes back, not the query text.
# `_neo4j` is the canonical attribute the contracts source mounts; reaching
# into it to swap the session is how the existing mocks pin the wire shape.
# pylint: disable=protected-access,unused-argument
from unittest.mock import MagicMock


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
