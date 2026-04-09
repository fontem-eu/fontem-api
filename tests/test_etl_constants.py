"""
Tests for ETL module constants — kills mutants on configuration values.

These tests verify that critical constants (batch sizes, exchange rates,
taxonomy codes, URLs) haven't been accidentally mutated.
"""
# pylint: disable=missing-function-docstring,missing-class-docstring
from src.etl.load_ted_contracts import BATCH_SIZE as TED_BATCH, _EUR_RATES
from src.etl.load_eu_lobbying import (
    BATCH_SIZE as LOBBY_BATCH, TR_XML_URL,
    CONSTRAINT_CYPHER, MERGE_LOBBYIST, MATCH_COMPANY,
)
from src.etl.load_cpv import CPV_DIVISIONS
from src.etl.load_us_companies import BATCH_SIZE as US_BATCH


class TestTedContractConstants:
    def test_batch_size(self):
        assert TED_BATCH == 500

    def test_eur_rate_is_1(self):
        assert _EUR_RATES["EUR"] == 1.0

    def test_usd_rate(self):
        assert _EUR_RATES["USD"] == 0.92

    def test_gbp_rate(self):
        assert _EUR_RATES["GBP"] == 1.17

    def test_chf_rate(self):
        assert _EUR_RATES["CHF"] == 1.05

    def test_jpy_rate(self):
        assert _EUR_RATES["JPY"] == 0.0061

    def test_pln_rate(self):
        assert _EUR_RATES["PLN"] == 0.23

    def test_all_rates_positive(self):
        for currency, rate in _EUR_RATES.items():
            assert rate > 0, f"{currency} rate should be positive"


class TestLobbyingConstants:
    def test_batch_size(self):
        assert LOBBY_BATCH == 500

    def test_url_is_correct(self):
        assert "transparency-register.europa.eu" in TR_XML_URL
        assert TR_XML_URL.endswith("_en")

    def test_constraint_cypher_creates_lobbyist_constraint(self):
        assert "Lobbyist" in CONSTRAINT_CYPHER
        assert "tr_id" in CONSTRAINT_CYPHER
        assert "UNIQUE" in CONSTRAINT_CYPHER

    def test_merge_lobbyist_uses_tr_id(self):
        assert "tr_id" in MERGE_LOBBYIST
        assert "MERGE" in MERGE_LOBBYIST

    def test_match_company_cypher_exists(self):
        assert "Company" in MATCH_COMPANY or "MATCH" in MATCH_COMPANY


class TestCPVDivisions:
    def test_has_standard_divisions(self):
        assert "03" in CPV_DIVISIONS  # Agricultural
        assert "45" in CPV_DIVISIONS  # Construction
        assert "48" in CPV_DIVISIONS  # Software
        assert "72" in CPV_DIVISIONS  # IT services

    def test_agricultural_label(self):
        assert "Agricultural" in CPV_DIVISIONS["03"]

    def test_construction_label(self):
        assert "Construction" in CPV_DIVISIONS["45"]

    def test_software_label(self):
        assert "Software" in CPV_DIVISIONS["48"]

    def test_division_count(self):
        # Should have ~45 top-level divisions
        assert len(CPV_DIVISIONS) >= 40


class TestUSCompaniesConstants:
    def test_batch_size(self):
        assert US_BATCH == 2000
