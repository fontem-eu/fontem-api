"""Unit tests for the Engine's dispatch logic.

These tests use a mock Persistence so we don't need a live DB. The
real Postgres integration is covered by
test_reasoner_persistence_integration.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.reasoner.engine import Engine
from src.reasoner.rule import NEVER_AUTO_APPLY, Finding, RuleContext


# ── Rule stubs ───────────────────────────────────────────────────

class ReviewOnlyRule:
    id = "review-only"
    description = "Never auto-applies — findings persist."
    severity = "warning"
    auto_apply_threshold = NEVER_AUTO_APPLY
    rule_categories: list[str] = []

    def __init__(self, findings: list[Finding]) -> None:
        self._findings = findings

    def evaluate(self, ctx):  # pylint: disable=unused-argument
        return iter(self._findings)


class AutoApplyRule:
    id = "auto-apply"
    description = "Auto-applies when confidence >= 0.8."
    severity = "warning"
    auto_apply_threshold = 0.8
    rule_categories: list[str] = []

    def __init__(self, findings: list[Finding]) -> None:
        self._findings = findings
        self.applied: list[Finding] = []

    def evaluate(self, ctx):  # pylint: disable=unused-argument
        return iter(self._findings)

    def apply(self, ctx, finding):  # pylint: disable=unused-argument
        self.applied.append(finding)


# ── Helpers ──────────────────────────────────────────────────────

def _ctx(**kwargs):
    return RuleContext(
        neo4j=MagicMock(), run_id="run-x", target_ids=None, **kwargs,
    )


def _finding(rule_id, confidence=1.0, target_ids=("t1",)):
    return Finding(
        rule_id=rule_id,
        severity="warning",
        confidence=confidence,
        target_ids=list(target_ids),
        message="test",
    )


# ── Tests ────────────────────────────────────────────────────────

def test_review_only_rule_persists_everything():
    persistence = MagicMock()
    persistence.upsert_many.return_value = 2
    rule = ReviewOnlyRule([_finding("review-only"), _finding("review-only", target_ids=("t2",))])
    result = Engine(persistence).run_rule(rule, _ctx())
    persistence.upsert_many.assert_called_once()
    assert result.findings_seen == 2
    assert result.findings_persisted == 2
    assert result.findings_auto_applied == 0
    persistence.record_audit.assert_not_called()


def test_auto_apply_fires_and_audits_only_when_above_threshold():
    persistence = MagicMock()
    persistence.upsert_many.return_value = 1
    rule = AutoApplyRule([
        _finding("auto-apply", confidence=0.95),  # auto-apply
        _finding("auto-apply", confidence=0.5, target_ids=("t2",)),  # persist
    ])
    result = Engine(persistence).run_rule(rule, _ctx())
    assert result.findings_seen == 2
    assert result.findings_auto_applied == 1
    assert result.findings_persisted == 1
    assert len(rule.applied) == 1
    assert rule.applied[0].confidence == 0.95
    persistence.record_audit.assert_called_once()
    persistence.mark_applied.assert_called_once()


def test_dry_run_does_not_touch_persistence_or_apply():
    persistence = MagicMock()
    rule = AutoApplyRule([_finding("auto-apply", confidence=0.99)])
    result = Engine(persistence).run_rule(rule, _ctx(dry_run=True))
    assert result.findings_seen == 1
    # No writes in dry-run
    persistence.upsert_many.assert_not_called()
    persistence.record_audit.assert_not_called()
    persistence.mark_applied.assert_not_called()
    # apply() is NOT invoked in dry-run either — we just log what would happen
    assert rule.applied == []


def test_apply_exception_falls_back_to_persistence():
    class BrokenApplyRule(AutoApplyRule):
        def apply(self, ctx, finding):
            raise RuntimeError("boom")

    persistence = MagicMock()
    persistence.upsert_many.return_value = 1
    rule = BrokenApplyRule([_finding("auto-apply", confidence=1.0)])
    result = Engine(persistence).run_rule(rule, _ctx())
    assert result.findings_seen == 1
    assert result.findings_auto_applied == 0
    assert result.findings_persisted == 1
    assert result.errors and "boom" in result.errors[0]


def test_evaluate_exception_is_captured_not_raised():
    class BrokenEvaluateRule:
        id = "broken"
        description = "Raises in evaluate."
        severity = "error"
        auto_apply_threshold = NEVER_AUTO_APPLY
        rule_categories = []

        def evaluate(self, ctx):
            raise RuntimeError("blew up mid-scan")

    persistence = MagicMock()
    result = Engine(persistence).run_rule(BrokenEvaluateRule(), _ctx())
    assert result.findings_seen == 0
    assert result.errors and "blew up" in result.errors[0]
    persistence.upsert_many.assert_not_called()
