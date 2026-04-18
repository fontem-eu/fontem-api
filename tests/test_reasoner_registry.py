"""Unit tests for Registry loading behaviour."""
from __future__ import annotations

import sys
import types

import pytest

from src.reasoner.registry import Registry
from src.reasoner.rule import NEVER_AUTO_APPLY


class _StubRule:
    def __init__(self, rid: str) -> None:
        self.id = rid
        self.description = f"stub {rid}"
        self.severity = "info"
        self.auto_apply_threshold = NEVER_AUTO_APPLY
        self.rule_categories: list[str] = []

    def evaluate(self, ctx):  # pylint: disable=unused-argument
        return []


def _make_module(name: str, rule_obj) -> None:
    mod = types.ModuleType(name)
    mod.RULE = rule_obj
    sys.modules[name] = mod


def test_registry_loads_declared_modules(request):
    _make_module("_test_rule_mod_a", _StubRule("alpha"))
    _make_module("_test_rule_mod_b", _StubRule("beta"))
    request.addfinalizer(lambda: sys.modules.pop("_test_rule_mod_a", None))
    request.addfinalizer(lambda: sys.modules.pop("_test_rule_mod_b", None))

    reg = Registry(module_paths=["_test_rule_mod_a", "_test_rule_mod_b"])
    ids = sorted(r.id for r in reg.all())
    assert ids == ["alpha", "beta"]


def test_registry_by_ids_raises_on_unknown(request):
    _make_module("_test_rule_mod_c", _StubRule("gamma"))
    request.addfinalizer(lambda: sys.modules.pop("_test_rule_mod_c", None))

    reg = Registry(module_paths=["_test_rule_mod_c"])
    with pytest.raises(KeyError, match="nope"):
        reg.by_ids(["gamma", "nope"])


def test_registry_tolerates_missing_module(caplog):
    # Non-existent module should be logged but not raise.
    reg = Registry(module_paths=["not.a.real.module.xxx"])
    assert list(reg.all()) == []
    assert "Failed to import rule module" in caplog.text


def test_registry_tolerates_module_without_rule(request, caplog):
    mod = types.ModuleType("_test_mod_no_rule")
    sys.modules["_test_mod_no_rule"] = mod
    request.addfinalizer(lambda: sys.modules.pop("_test_mod_no_rule", None))

    reg = Registry(module_paths=["_test_mod_no_rule"])
    assert list(reg.all()) == []
    assert "no RULE constant" in caplog.text
