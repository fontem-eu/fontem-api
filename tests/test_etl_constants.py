"""
Tests for ETL module constants — kills mutants on configuration values.

These tests verify that critical constants (URLs, taxonomy codes)
haven't been accidentally mutated. Per-loader batch sizes and
direct-Cypher constants were removed when the loaders moved to
the event log; the gmr-events EventLog handles transaction
grouping internally and the sinks own the projection.
"""
# pylint: disable=missing-function-docstring,missing-class-docstring
from src.etl._hooks import CONSOLIDATOR_URL
from src.etl.load_eu_lobbying import EMIT_CHUNK as LOBBY_CHUNK, TR_XML_URL


class TestConsolidatorURL:
    def test_default_uses_namespace_relative_dns(self):
        # Relative DNS so the same default works in fontem-shared and
        # fontem-prod without per-env overrides (the resolver expands
        # the bare host to <name>.<pod-ns>.svc.cluster.local). The
        # previous default `gmr-consolidator.gmr.svc.cluster.local`
        # doesn't resolve in the current cluster, so /resolve calls
        # silently degraded to "no_match" in every ETL.
        assert CONSOLIDATOR_URL == "http://fontem-consolidator:8000"

    def test_no_legacy_gmr_namespace(self):
        # Catches a future accidental revert to the old gmr-* shape.
        assert "gmr-consolidator" not in CONSOLIDATOR_URL
        assert ".gmr." not in CONSOLIDATOR_URL


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
