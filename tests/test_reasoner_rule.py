"""Unit tests for the Rule protocol + Finding dataclass."""
from __future__ import annotations

from src.reasoner.rule import Finding, NEVER_AUTO_APPLY, Rule, RuleContext


def test_finding_key_is_stable_for_same_targets_in_any_order():
    a = Finding(
        rule_id="r", severity="warning", confidence=0.9,
        target_ids=["t1", "t2", "t3"], message="m",
    )
    b = Finding(
        rule_id="r", severity="warning", confidence=0.9,
        target_ids=["t3", "t1", "t2"], message="different msg",
    )
    # Same rule + same (unordered) targets → same finding_key
    assert a.finding_key() == b.finding_key()


def test_finding_key_differs_across_rules():
    a = Finding(
        rule_id="rule-a", severity="warning", confidence=1.0,
        target_ids=["t"], message="",
    )
    b = Finding(
        rule_id="rule-b", severity="warning", confidence=1.0,
        target_ids=["t"], message="",
    )
    assert a.finding_key() != b.finding_key()


def test_finding_key_differs_across_target_sets():
    a = Finding(
        rule_id="r", severity="warning", confidence=1.0,
        target_ids=["t1"], message="",
    )
    b = Finding(
        rule_id="r", severity="warning", confidence=1.0,
        target_ids=["t1", "t2"], message="",
    )
    assert a.finding_key() != b.finding_key()


def test_protocol_accepts_minimal_rule_shape():
    class R:
        id = "x"
        description = "x"
        severity = "info"
        auto_apply_threshold = NEVER_AUTO_APPLY
        rule_categories: list[str] = []

        def evaluate(self, ctx):
            return []

    rule: Rule = R()  # type check: instance satisfies the protocol
    assert rule.id == "x"


def test_rule_context_defaults():
    ctx = RuleContext(neo4j=object(), run_id="run-1")
    assert ctx.target_ids is None
    assert ctx.dry_run is False
