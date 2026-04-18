"""Tests for the duplicate-company-by-vat rule."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.reasoner.rule import Finding, RuleContext
from src.reasoner.rules.duplicate_company_by_vat import RULE


def _make_ctx(rows, target_ids=None):
    """Neo4j mock whose session.run() returns `rows` once for evaluate()."""
    neo4j = MagicMock()
    session = MagicMock()
    neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    neo4j.session.return_value.__exit__ = MagicMock(return_value=False)
    session.run.return_value = iter(rows)
    ctx = RuleContext(neo4j=neo4j, run_id="r1", target_ids=target_ids)
    return ctx, session


def test_flags_shared_vat_cluster_as_single_finding():
    rows = [{
        "vat": "FR12345",
        "ids": ["c1", "c2", "c3"],
        "names": ["Acme FR", "Acme France", "ACME FR SA"],
        "countries": ["FRA", "FRA", "FRA"],
    }]
    ctx, _ = _make_ctx(rows)

    findings = list(RULE.evaluate(ctx))

    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "duplicate-company-by-vat"
    assert f.severity == "warning"
    assert f.confidence == 0.9
    assert set(f.target_ids) == {"c1", "c2", "c3"}
    assert "FR12345" in f.message
    assert f.payload["vat_number"] == "FR12345"
    assert f.payload["names"] == ["Acme FR", "Acme France", "ACME FR SA"]
    assert f.payload["countries"] == ["FRA", "FRA", "FRA"]


def test_full_sweep_uses_unfiltered_query():
    ctx, session = _make_ctx([])
    list(RULE.evaluate(ctx))

    session.run.assert_called_once()
    args, kwargs = session.run.call_args
    assert "WHERE c.vat_number IS NOT NULL" in args[0]
    # Full sweep passes no params.
    assert kwargs == {}


def test_targeted_mode_sends_ids():
    rows = [{
        "vat": "DE999", "ids": ["c1", "c5"],
        "names": ["A", "B"], "countries": ["DEU", "DEU"],
    }]
    ctx, session = _make_ctx(rows, target_ids=["c1"])
    list(RULE.evaluate(ctx))

    args, kwargs = session.run.call_args
    assert "UNWIND $ids" in args[0]
    assert kwargs == {"ids": ["c1"]}


def test_rule_is_review_only_by_default():
    # Even though apply() exists, the threshold (1.1) means no finding
    # from this rule will trigger auto-apply (confidence maxes at 0.9).
    assert hasattr(RULE, "apply")
    assert RULE.auto_apply_threshold > 0.9


def test_apply_creates_pairwise_same_as_edges():
    """Three companies → three pairs (not four, not a star)."""
    neo4j = MagicMock()
    session = MagicMock()
    neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    neo4j.session.return_value.__exit__ = MagicMock(return_value=False)
    ctx = RuleContext(neo4j=neo4j, run_id="r1")

    finding = Finding(
        rule_id="duplicate-company-by-vat",
        severity="warning",
        confidence=0.95,
        target_ids=["c3", "c1", "c2"],  # unsorted on purpose
        message="test",
    )
    RULE.apply(ctx, finding)

    args, kwargs = session.run.call_args
    assert "MERGE (a)-[r:SAME_AS]-(b)" in args[0]
    pairs = kwargs["pairs"]
    # Sorted ids → predictable pairs
    assert pairs == [
        {"a": "c1", "b": "c2"},
        {"a": "c1", "b": "c3"},
        {"a": "c2", "b": "c3"},
    ]
    assert kwargs["confidence"] == 0.95


def test_apply_is_noop_for_single_target():
    neo4j = MagicMock()
    ctx = RuleContext(neo4j=neo4j, run_id="r1")
    RULE.apply(
        ctx,
        Finding(
            rule_id="duplicate-company-by-vat",
            severity="warning",
            confidence=0.9,
            target_ids=["c1"],
            message="",
        ),
    )
    # No session opened for a degenerate cluster of 1
    neo4j.session.assert_not_called()
