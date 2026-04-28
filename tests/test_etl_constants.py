"""
Tests for ETL module constants — kills mutants on configuration values.

These tests verify that critical constants (batch sizes, exchange rates,
taxonomy codes, URLs) haven't been accidentally mutated.
"""
# pylint: disable=missing-function-docstring,missing-class-docstring
from src.etl.load_ted_contracts import BATCH_SIZE as TED_BATCH
from src.etl.load_eu_lobbying import (
    BATCH_SIZE as LOBBY_BATCH, TR_XML_URL,
    CONSTRAINT_CYPHER, MERGE_LOBBYIST, MERGE_REPRESENTS,
)
from src.etl.load_cpv import CPV_DIVISIONS
from src.etl.load_us_companies import BATCH_SIZE as US_BATCH


class TestTedContractConstants:
    def test_batch_size(self):
        assert TED_BATCH == 500


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

    def test_merge_represents_uses_resolver_gmr_id(self):
        # The old MATCH_COMPANY did fulltext name matching directly;
        # it has been replaced by /resolve. Edges are now created from
        # a gmr_id supplied by the resolver, NOT by name matching here.
        assert "MERGE (l)-[r:REPRESENTS]->(c)" in MERGE_REPRESENTS
        assert "row.gmr_id" in MERGE_REPRESENTS


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
