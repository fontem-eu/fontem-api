"""Tests for the TED company matcher."""
from unittest.mock import MagicMock

from src.etl.ted_matcher import TedMatcher


def _mock_session(vat_cache=None):
    """Create a mock Neo4j session.

    The first session.run() call (warm cache) returns the vat_cache entries.
    Subsequent calls return a MagicMock with .single() returning None.
    """
    session = MagicMock()

    cache_records = []
    if vat_cache:
        for vat, gid in vat_cache.items():
            r = MagicMock()
            r.__getitem__ = lambda self, k, v=vat, g=gid: (
                v if k == "vat" else g
            )
            cache_records.append(r)

    # First call: warm cache (iterable). Rest: standard MagicMock.
    cache_result = MagicMock()
    cache_result.__iter__ = MagicMock(return_value=iter(cache_records))
    default_result = MagicMock()
    default_result.single.return_value = None

    call_count = {"n": 0}

    def _run_side_effect(*_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return cache_result
        return default_result

    session.run = MagicMock(side_effect=_run_side_effect)
    return session


def test_layer1_vat_cache_hit():
    """Layer 1: cached VAT matches immediately."""
    session = _mock_session(vat_cache={"DE123": "gid-cached"})
    matcher = TedMatcher(session)
    result = matcher.match_company("SAP SE", "DE", vat="DE123")
    assert result.gmr_id == "gid-cached"
    assert result.layer == 1
    assert result.confidence == 1.0


def test_layer5_creates_new_with_vat():
    """Layer 5: unknown company with VAT creates new node."""
    session = _mock_session()
    matcher = TedMatcher(session)
    result = matcher.match_company("Unknown Corp", "FR", vat="FR999")
    assert result.layer == 5
    assert result.created_new is True
    assert result.gmr_id  # should be a valid UUID string


def test_layer5_creates_new_without_vat():
    """Layer 5: unknown company without VAT falls back to name."""
    session = _mock_session()
    matcher = TedMatcher(session)
    result = matcher.match_company("Mystery Inc", "US")
    assert result.layer == 5
    assert result.created_new is True


def test_stats_tracking():
    """Match stats are tracked correctly."""
    session = _mock_session(vat_cache={"DE1": "gid1", "DE2": "gid2"})
    matcher = TedMatcher(session)
    matcher.match_company("A", "DE", vat="DE1")
    matcher.match_company("B", "DE", vat="DE2")
    summary = matcher.stats.summary()
    assert summary["total"] == 2
    assert summary["by_layer"][1] == 2


def test_authority_id_deterministic():
    """Authority IDs are deterministic for the same name + country."""
    session = _mock_session()
    matcher = TedMatcher(session)
    id1 = matcher.match_authority("Ministry of X", "DE")
    id2 = matcher.match_authority("Ministry of X", "DE")
    assert id1 == id2


def test_authority_id_differs_by_country():
    """Different countries produce different authority IDs."""
    session = _mock_session()
    matcher = TedMatcher(session)
    id_de = matcher.match_authority("Ministry of X", "DE")
    id_fr = matcher.match_authority("Ministry of X", "FR")
    assert id_de != id_fr
