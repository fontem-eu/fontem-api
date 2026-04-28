"""DSN normalisation + bad-env detection for FontemStatsSource."""
from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access,unsupported-membership-test

from src.atlas_api.sources.fontem_stats import FontemStatsSource


def test_none_dsn_unconfigured():
    src = FontemStatsSource(None)
    assert not src.configured
    assert src.health().status == "unconfigured"
    assert "not set" in src.health().detail


def test_asyncpg_dialect_stripped():
    src = FontemStatsSource("postgresql+asyncpg://u:p@h:5432/d")
    assert src.configured
    assert src._dsn == "postgresql://u:p@h:5432/d"


def test_unsubstituted_dollar_paren_flagged():
    """If the K8s pod-spec env ordering leaves a `$(VAR)` reference
    untouched, the source must surface that as a clear configuration
    error rather than letting libpq emit an opaque 28P01."""
    src = FontemStatsSource("postgresql://u:$(POSTGRES_PASSWORD)@h:5432/d")
    assert not src.configured
    h = src.health()
    assert h.status == "unconfigured"
    assert "$(VAR)" in h.detail
    assert "Reorder" in h.detail


def test_substituted_dsn_passes():
    src = FontemStatsSource("postgresql://u:realpw@h:5432/d")
    assert src.configured
    # Health probe will fail (no real db here) but the DSN is accepted.
    assert "$(VAR)" not in (src.health().detail or "")
