"""Rule discovery.

Rules live in ``src/reasoner/rules/``. Each module exports a single
``RULE`` constant — an instance of a class that satisfies the ``Rule``
protocol. The registry walks the package and collects them.

Explicit-list style (rather than auto-import everything) because:
  - Import order determines which rule wins if two declare the same id
  - Disabled rules should be obvious in a code review
  - Tests pin exactly the rules they exercise
"""
from __future__ import annotations

import importlib
import logging
from typing import Iterable

from .rule import Rule

logger = logging.getLogger(__name__)


# Explicit list of modules that expose a RULE constant. Order = priority.
# Add new rule modules here after adding tests + documentation.
_BUILTIN_RULE_MODULES: list[str] = [
    # Populated as rules land. The first two ship in follow-up PRs:
    # "src.reasoner.rules.orphan_company",
    # "src.reasoner.rules.duplicate_company_by_vat",
]


class Registry:
    def __init__(self, module_paths: list[str] | None = None) -> None:
        self._module_paths = module_paths or list(_BUILTIN_RULE_MODULES)
        self._rules: dict[str, Rule] = {}
        self._load()

    def _load(self) -> None:
        for path in self._module_paths:
            try:
                mod = importlib.import_module(path)
            except ImportError as exc:
                logger.error("Failed to import rule module %s: %s", path, exc)
                continue
            rule = getattr(mod, "RULE", None)
            if rule is None:
                logger.error("%s has no RULE constant — skipping", path)
                continue
            if rule.id in self._rules:
                logger.warning(
                    "Duplicate rule id %r (second declaration in %s wins)",
                    rule.id, path,
                )
            self._rules[rule.id] = rule

    def all(self) -> Iterable[Rule]:
        return self._rules.values()

    def by_ids(self, ids: list[str]) -> list[Rule]:
        missing = [i for i in ids if i not in self._rules]
        if missing:
            raise KeyError(f"Unknown rule id(s): {missing}")
        return [self._rules[i] for i in ids]
