"""End-to-end tests for the Public Spending router."""
from unittest.mock import MagicMock

from tests.dishka_fixtures import make_test_client, cleanup_dishka


def _mock_recs(companies=None, authorities=None):
    src = MagicMock()
    src.top_companies_in_country   = MagicMock(return_value=companies   or [])
    src.top_authorities_in_country = MagicMock(return_value=authorities or [])
    return src


def test_recommendations_returns_company_and_authority_lists():
    companies = [
        {"id": "c1", "name": "Foo Lda", "total_value_eur": 1.0e6,
         "contract_count": 5},
    ]
    authorities = [
        {"id": "a1", "name": "Município X", "total_value_eur": 5.0e6,
         "contract_count": 12},
    ]
    rec = _mock_recs(companies=companies, authorities=authorities)
    client = make_test_client(recommendations_source=rec)
    try:
        r = client.get("/euro-tracker/recommendations?country=PRT&limit=10")
        assert r.status_code == 200
        body = r.json()
        assert body["country"]    == "PRT"
        assert body["companies"]  == companies
        assert body["authorities"] == authorities
        # Limit was forwarded.
        rec.top_companies_in_country.assert_called_once_with("PRT", limit=10)
        rec.top_authorities_in_country.assert_called_once_with("PRT", limit=10)
    finally:
        cleanup_dishka()


def test_recommendations_uppercases_country_code():
    rec = _mock_recs()
    client = make_test_client(recommendations_source=rec)
    try:
        r = client.get("/euro-tracker/recommendations?country=prt")
        assert r.status_code == 200
        rec.top_companies_in_country.assert_called_once_with("PRT", limit=10)
    finally:
        cleanup_dishka()


def test_recommendations_rejects_non_alpha3():
    client = make_test_client(recommendations_source=_mock_recs())
    try:
        # min_length=3 / max_length=3 enforced by FastAPI Query.
        assert client.get("/euro-tracker/recommendations?country=PT").status_code == 422
        assert client.get("/euro-tracker/recommendations?country=PORT").status_code == 422
    finally:
        cleanup_dishka()


def test_me_country_returns_unknown_when_geoip_db_missing():
    """Default mock service points at /nonexistent.mmdb → unavailable;
    response should report `country: null`, `source: unknown`, and
    surface the reason for debugging."""
    client = make_test_client()
    try:
        r = client.get("/euro-tracker/me/country")
        assert r.status_code == 200
        body = r.json()
        assert body["country"] is None
        assert body["source"]  == "unknown"
        assert body["geoip_unavailable_reason"]
    finally:
        cleanup_dishka()


def test_me_country_with_mock_service_returns_country():
    """When the IP-to-country service yields a country, the endpoint
    surfaces it. Mock the service entirely so we don't depend on
    a real GeoIP DB in CI."""
    fake = MagicMock()
    fake.available = True
    fake.unavailable_reason = None
    fake.lookup = MagicMock(return_value="DEU")
    client = make_test_client(ip_to_country=fake)
    try:
        r = client.get(
            "/euro-tracker/me/country",
            headers={"X-Forwarded-For": "5.6.7.8"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["country"] == "DEU"
        assert body["source"]  == "geoip"
        # Lookup was given the IP from XFF (not the test client's
        # 127.0.0.1).
        fake.lookup.assert_called_once_with("5.6.7.8")
    finally:
        cleanup_dishka()
