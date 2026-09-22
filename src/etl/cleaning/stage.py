"""``run_stage``: every rule over one notice, folded into one result.

The stage is the only thing the loader calls. It owns the rule order
(``rules/__init__.py``), decides which rule a multiply-hit supplier is
withheld under (the first), threads the corrected value facts from one
value rule into the next, and counts.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .facts import NoticeFacts
from .lookups import Lookups
from .outcomes import (
    IdentifierNormalised, Keep, Outcome, Quarantine, Rescale,
    SupplierWithheld, ValueDecision, ValueQuarantined, ValueRescaled,
)
from .rules import DATE_RULES, IDENTIFIER_RULES, NAME_RULES, VALUE_RULES
from .rules.identifiers import base_identifier


@dataclass(frozen=True)
class CleaningResult:
    """What the loader applies."""

    # org_id -> rule id the supplier was withheld under
    withheld: dict[str, str] = field(default_factory=dict)
    # org_id -> canonical VAT (or None) for EVERY organisation on the
    # notice, buyer included; the matcher reads this instead of the raw id
    identifiers: dict[str, str | None] = field(default_factory=dict)
    value: ValueDecision = field(default_factory=Keep)
    # every rule id that fired, in firing order, each once
    rules_fired: tuple[str, ...] = ()
    # rule id -> number of outcomes (a rule that hit two suppliers counts 2)
    counters: dict[str, int] = field(default_factory=dict)
    outcomes: tuple[Outcome, ...] = ()

    @property
    def fired(self) -> bool:
        return bool(self.rules_fired)


def _run(rules, facts: NoticeFacts, lookups: Lookups) -> list[Outcome]:
    out: list[Outcome] = []
    for rule in rules:
        if rule.applies(facts, lookups):
            out.extend(rule.decide(facts, lookups))
    return out


def _run_value_rules(facts: NoticeFacts, lookups: Lookups,
                     ) -> tuple[list[Outcome], ValueDecision]:
    """Sequential: a rescale feeds the next rule; a quarantine ends it."""
    outcomes: list[Outcome] = []
    decision: ValueDecision = Keep()
    current = facts
    for rule in VALUE_RULES:
        if not rule.applies(current, lookups):
            continue
        for outcome in rule.decide(current, lookups):
            outcomes.append(outcome)
            if isinstance(outcome, ValueRescaled):
                decision = Rescale(outcome.factor, outcome.tier,
                                   outcome.detail, outcome.corrected)
                current = current.with_value(outcome.corrected)
            elif isinstance(outcome, ValueQuarantined):
                return outcomes, Quarantine(
                    outcome.reason, outcome.detail, outcome.candidates)
    return outcomes, decision


def run_stage(facts: NoticeFacts, lookups: Lookups | None = None) -> CleaningResult:
    """Run every rule over the facts of one notice."""
    lookups = lookups or Lookups()
    outcomes = _run(NAME_RULES, facts, lookups)
    outcomes += _run(IDENTIFIER_RULES, facts, lookups)
    outcomes += _run(DATE_RULES, facts, lookups)
    value_outcomes, decision = _run_value_rules(facts, lookups)
    outcomes += value_outcomes

    withheld: dict[str, str] = {}
    identifiers = {o.org_id: base_identifier(o) for o in facts.organizations}
    for outcome in outcomes:
        if isinstance(outcome, SupplierWithheld):
            withheld.setdefault(outcome.subject, outcome.rule_id)
        elif isinstance(outcome, IdentifierNormalised):
            identifiers[outcome.subject] = outcome.canonical

    fired: list[str] = []
    for outcome in outcomes:
        if outcome.rule_id not in fired:
            fired.append(outcome.rule_id)
    return CleaningResult(
        withheld=withheld, identifiers=identifiers, value=decision,
        rules_fired=tuple(fired),
        counters=dict(Counter(o.rule_id for o in outcomes)),
        outcomes=tuple(outcomes),
    )
