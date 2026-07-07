"""Tests for the TED company matcher."""
from unittest.mock import MagicMock, patch

from src.etl._hooks import ResolveMatch, ResolveResult
from src.etl.ted_matcher import (
    FUZZY_ACCEPT_CONF, MatcherStats, TedMatcher,
)


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
    """Layer 5: unknown company with VAT creates new node when /resolve
    returns no match."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch("src.etl.ted_matcher.resolve_entity", return_value=None):
        result = matcher.match_company("Unknown Corp", "FR", vat="FR999")
    assert result.layer == 5
    assert result.created_new is True
    assert result.gmr_id  # should be a valid UUID string


def test_layer5_creates_new_without_vat():
    """Layer 5: unknown company without VAT falls back to name."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch("src.etl.ted_matcher.resolve_entity", return_value=None):
        result = matcher.match_company("Mystery Inc", "US")
    assert result.layer == 5
    assert result.created_new is True


def test_stats_tracking():
    """Match stats are tracked correctly. Two cache hits → both Layer 1."""
    session = _mock_session(vat_cache={"DE1": "gid1", "DE2": "gid2"})
    matcher = TedMatcher(session)
    # Cache hits skip /resolve entirely, so no patching needed.
    matcher.match_company("A", "DE", vat="DE1")
    matcher.match_company("B", "DE", vat="DE2")
    summary = matcher.stats.summary()
    assert summary["total"] == 2
    assert summary["by_layer"][1] == 2


def test_authority_id_deterministic():
    """Authority IDs are deterministic for the same name + country
    when no resolver match exists."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch("src.etl.ted_matcher.resolve_entity", return_value=None):
        id1 = matcher.match_authority("Ministry of X", "DE")
        id2 = matcher.match_authority("Ministry of X", "DE")
    assert id1 == id2


def test_authority_id_differs_by_country():
    """Different countries produce different authority IDs."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch("src.etl.ted_matcher.resolve_entity", return_value=None):
        id_de = matcher.match_authority("Ministry of X", "DE")
        id_fr = matcher.match_authority("Ministry of X", "FR")
    assert id_de != id_fr


# ─────────────────────────────────────────────────────────────────────
# /resolve integration — Layer 2 (deterministic) and Layer 3 (fuzzy
# accept) and the fall-through to Layer 5 on ambiguous low-conf.
# ─────────────────────────────────────────────────────────────────────


def _resolve_match(gmr_id_value, tier, conf):
    return ResolveResult(
        hint="matched",
        match=ResolveMatch(
            gmr_id=gmr_id_value, name="x", country="DEU", lei=None,
            tier=tier, confidence=conf,
        ),
        candidates=[],
        normalised_country="DEU",
    )


def _resolve_ambiguous(top_conf, second_conf=None):
    candidates = [
        ResolveMatch(gmr_id="top", name="A", country="DEU", lei=None,
                     tier="fuzzy", confidence=top_conf),
    ]
    if second_conf is not None:
        candidates.append(
            ResolveMatch(gmr_id="second", name="B", country="DEU", lei=None,
                         tier="fuzzy", confidence=second_conf),
        )
    return ResolveResult(
        hint="ambiguous", match=None, candidates=candidates,
        normalised_country="DEU",
    )


def test_layer2_resolver_lei_match():
    """When /resolve returns a hard-id match, layer is 2."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch(
        "src.etl.ted_matcher.resolve_entity",
        return_value=_resolve_match("gmr-lei", "lei", 1.0),
    ):
        result = matcher.match_company("Some Corp", "DE", vat="DE99")
    assert result.layer == 2
    assert result.gmr_id == "gmr-lei"
    assert result.resolver_tier == "lei"


def test_layer2_resolver_name_country_match():
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch(
        "src.etl.ted_matcher.resolve_entity",
        return_value=_resolve_match("gmr-nc", "name_country", 0.95),
    ):
        result = matcher.match_company("Long Specific Name Inc", "DE")
    assert result.layer == 2
    assert result.resolver_tier == "name_country"


def test_layer3_resolver_fuzzy_single_high_confidence_accepted():
    """Single fuzzy candidate above the floor — accept it (matches the
    old Dice>0.85 behaviour but with the resolver's country guard)."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch(
        "src.etl.ted_matcher.resolve_entity",
        return_value=_resolve_ambiguous(top_conf=0.92),
    ):
        result = matcher.match_company("Borderline Match GmbH", "DE")
    assert result.layer == 3
    assert result.gmr_id == "top"
    assert result.confidence >= FUZZY_ACCEPT_CONF


def test_fuzzy_floor_is_090():
    """The floor is 0.90 (eval decision for #270). 0.95 was rejected: it
    sits above the resolver's 0.94 fuzzy cap and would delete the tier."""
    assert FUZZY_ACCEPT_CONF == 0.90


def test_layer5_when_fuzzy_between_old_and_new_floor():
    """#270 tightening: a lone fuzzy candidate at 0.87 — accepted under
    the old 0.85 floor — now falls through to a new node rather than
    guess a homonym link (AGILIS/SCORE class)."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch(
        "src.etl.ted_matcher.resolve_entity",
        return_value=_resolve_ambiguous(top_conf=0.87),
    ):
        result = matcher.match_company("Homonym Match SA", "FR")
    assert result.layer == 5
    assert result.created_new is True


def test_layer5_when_fuzzy_top_below_floor():
    """A weak fuzzy hit must NOT auto-match — fall through to Layer 5."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch(
        "src.etl.ted_matcher.resolve_entity",
        return_value=_resolve_ambiguous(top_conf=0.50),
    ):
        result = matcher.match_company("Weak Fuzzy Match", "DE")
    assert result.layer == 5
    assert result.created_new is True


def test_layer5_when_two_fuzzy_above_floor():
    """Two candidates both above the floor means we can't pick one
    deterministically — fall through to Layer 5 rather than guess."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch(
        "src.etl.ted_matcher.resolve_entity",
        return_value=_resolve_ambiguous(top_conf=0.92, second_conf=0.90),
    ):
        result = matcher.match_company("Ambiguous Match", "DE")
    assert result.layer == 5


def test_layer5_when_resolver_unavailable():
    """Transport failure → resolver_failures stat increments and we
    fall through to Layer 5 (silent miss > silent corruption)."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch("src.etl.ted_matcher.resolve_entity", return_value=None):
        result = matcher.match_company("Any Long Name Inc", "DE")
    assert result.layer == 5
    assert matcher.stats.resolver_failures == 1


def test_authority_resolver_match_short_circuits_uuid():
    """Authority resolution uses /resolve when available; falls back to
    deterministic UUID otherwise."""
    session = _mock_session()
    matcher = TedMatcher(session)
    with patch(
        "src.etl.ted_matcher.resolve_entity",
        return_value=_resolve_match("auth-existing", "name_country", 0.95),
    ):
        out = matcher.match_authority("Ministry of Existing", "DE")
    assert out == "auth-existing"


def test_matcher_stats_summary_shape():
    """summary() reports the per-layer histogram, total and resolver
    failures — the payload logged at the end of each TED run."""
    stats = MatcherStats()
    stats.record(2)
    stats.record(2)
    stats.record(3)
    stats.resolver_failures += 1
    summary = stats.summary()
    assert summary["total"] == 3
    assert summary["by_layer"][2] == 2
    assert summary["by_layer"][3] == 1
    assert summary["resolver_failures"] == 1
