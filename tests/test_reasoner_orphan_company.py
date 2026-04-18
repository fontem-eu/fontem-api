"""Tests for the orphan-company rule."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.reasoner.rule import RuleContext
from src.reasoner.rules.orphan_company import RULE


def _neo4j_returning_pages(pages: list[list[dict]]) -> MagicMock:
    """Build a mocked Neo4j client whose session returns the given pages
    in order for repeated .run() calls."""
    neo4j = MagicMock()
    session = MagicMock()
    neo4j.session.return_value.__enter__ = MagicMock(return_value=session)
    neo4j.session.return_value.__exit__ = MagicMock(return_value=False)
    session.run.side_effect = [iter(page) for page in pages]
    return neo4j


def test_full_sweep_paginates_and_yields_findings():
    page1 = [
        {"gmr_id": "c1", "name": "Acme", "country": "DEU", "source": "gleif"},
        {"gmr_id": "c2", "name": "Globex", "country": "FRA", "source": "gleif"},
    ]
    page2: list[dict] = []  # empty page ends pagination
    neo4j = _neo4j_returning_pages([page1, page2])

    ctx = RuleContext(neo4j=neo4j, run_id="r1")
    findings = list(RULE.evaluate(ctx))

    assert len(findings) == 2
    assert findings[0].rule_id == "orphan-company"
    assert findings[0].severity == "warning"
    assert findings[0].confidence == 1.0
    assert findings[0].target_ids == ["c1"]
    assert findings[0].message == "Orphan company: Acme"
    assert findings[0].payload == {
        "country": "DEU", "name": "Acme", "source": "gleif",
    }


def test_target_ids_mode_uses_targeted_cypher():
    neo4j = _neo4j_returning_pages([[
        {"gmr_id": "c42", "name": "X", "country": "ITA", "source": "gleif"},
    ]])
    ctx = RuleContext(neo4j=neo4j, run_id="r1", target_ids=["c42", "c99"])

    findings = list(RULE.evaluate(ctx))

    session = neo4j.session.return_value.__enter__.return_value
    session.run.assert_called_once()
    args, kwargs = session.run.call_args
    # The targeted query receives ids as a parameter, not SKIP/LIMIT.
    assert "UNWIND" in args[0]
    assert kwargs["ids"] == ["c42", "c99"]
    assert len(findings) == 1
    assert findings[0].target_ids == ["c42"]


def test_empty_graph_yields_nothing():
    neo4j = _neo4j_returning_pages([[]])
    ctx = RuleContext(neo4j=neo4j, run_id="r1")
    assert list(RULE.evaluate(ctx)) == []


def test_rule_is_review_only():
    # Confirms the rule doesn't claim an apply() method that would
    # cause the engine to auto-mutate the graph.
    assert not hasattr(RULE, "apply")
    assert RULE.auto_apply_threshold > 1.0


def test_finding_includes_fallback_message_when_name_is_null():
    neo4j = _neo4j_returning_pages([
        [{"gmr_id": "c1", "name": None, "country": "DEU", "source": "gleif"}],
        [],  # pagination end
    ])
    ctx = RuleContext(neo4j=neo4j, run_id="r1")
    findings = list(RULE.evaluate(ctx))
    # Nameless companies still get a usable message (falls back to gmr_id)
    assert findings[0].message == "Orphan company: c1"
