"""Smoke tests for the reasoner CLI — verifies argument parsing and
that --dry-run actually writes nothing. No live DB / Neo4j needed."""
from __future__ import annotations

import sys
import types

from src.reasoner import cli


def _stub_rule_module(name: str, rule_id: str) -> None:
    """Register a toy rule module in sys.modules."""
    mod = types.ModuleType(name)

    class _StubRule:
        id = rule_id
        description = "stub"
        severity = "info"
        auto_apply_threshold = 2.0
        rule_categories: list[str] = []

        def evaluate(self, ctx):  # pylint: disable=unused-argument
            return []

    mod.RULE = _StubRule()
    sys.modules[name] = mod


def test_list_rules_prints_registered_ids(capsys, monkeypatch):
    _stub_rule_module("_cli_test_rule_alpha", "alpha")
    monkeypatch.setattr(
        "src.reasoner.registry._BUILTIN_RULE_MODULES",
        ["_cli_test_rule_alpha"],
    )
    rc = cli.main(["list-rules"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out


def test_sweep_dry_run_returns_zero_with_empty_registry(monkeypatch):
    monkeypatch.setattr(
        "src.reasoner.registry._BUILTIN_RULE_MODULES", [],
    )
    rc = cli.main(["sweep", "--dry-run"])
    assert rc == 0


def test_sweep_dry_run_does_not_instantiate_persistence(monkeypatch):
    _stub_rule_module("_cli_test_rule_beta", "beta")
    monkeypatch.setattr(
        "src.reasoner.registry._BUILTIN_RULE_MODULES",
        ["_cli_test_rule_beta"],
    )

    class _Boom:
        def __init__(self):
            raise AssertionError("Persistence must NOT be built in --dry-run")

    # Force Neo4j client to something harmless
    monkeypatch.setattr("src.reasoner.cli.Neo4jClient", lambda *a, **kw: object())
    monkeypatch.setattr("src.reasoner.cli.Persistence", _Boom)

    rc = cli.main(["sweep", "--dry-run"])
    assert rc == 0
