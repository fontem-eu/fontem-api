"""
Tests for ETL module constants — kills mutants on configuration values.

These tests verify that critical constants (URLs, taxonomy codes)
haven't been accidentally mutated. Per-loader batch sizes and
direct-Cypher constants were removed when the loaders moved to
the event log; the gmr-events EventLog handles transaction
grouping internally and the sinks own the projection.
"""
# pylint: disable=missing-function-docstring,missing-class-docstring
from src.etl.load_eu_lobbying import EMIT_CHUNK as LOBBY_CHUNK, TR_XML_URL
from src.etl.load_cpv import CPV_DIVISIONS


class TestLobbyingConstants:
    def test_emit_chunk_size(self):
        # The trigger-side chunk size for per-batch event-log writes.
        # 500 keeps each Postgres transaction bounded but lets one
        # cron pass cover ~10k registrations in a small handful of
        # batches.
        assert LOBBY_CHUNK == 500

    def test_url_is_correct(self):
        assert "transparency-register.europa.eu" in TR_XML_URL
        assert TR_XML_URL.endswith("_en")


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


