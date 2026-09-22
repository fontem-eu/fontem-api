"""The rule protocol and the ordered registry the stage runs.

A rule is a small object with an ``id`` (the string that lands in
``cleaning_rules`` on the event), ``applies`` (a cheap guard) and
``decide`` (the outcomes for one notice). Rules are pure: they read the
``NoticeFacts`` and the injected ``Lookups`` and return outcomes; the
stage folds those into a ``CleaningResult`` and the loader applies it.

Order matters twice: a supplier hit by several name rules is withheld
under the FIRST rule in ``NAME_RULES`` (the most specific), and the
value rules run in sequence, each seeing the facts as corrected by the
previous one.
"""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from ..facts import NoticeFacts
from ..lookups import Lookups
from ..outcomes import Outcome
from .dates import DatePlaceholderRule
from .identifiers import NationalIdCountryPrefixedRule
from .names import (
    ItalianNoticeTextRule, MultipleAwardeesRule, NameContainsUrlRule,
    NameIsPlaceholderRule, NameIsSentenceRule,
)
from .values import (
    MilliEuroCountryPriorRule, MilliEuroSiblingRatioRule, ValuePeerOutlierRule,
)


@runtime_checkable
class Rule(Protocol):
    """What the stage needs from a rule."""

    id: str

    def applies(self, facts: NoticeFacts, lookups: Lookups) -> bool:
        """Cheap guard: is there anything for this rule to look at?"""

    def decide(self, facts: NoticeFacts, lookups: Lookups) -> Sequence[Outcome]:
        """The outcomes for this notice (empty when nothing fired)."""


NAME_RULES: tuple[Rule, ...] = (
    ItalianNoticeTextRule(),
    MultipleAwardeesRule(),
    NameContainsUrlRule(),
    NameIsPlaceholderRule(),
    NameIsSentenceRule(),
)

IDENTIFIER_RULES: tuple[Rule, ...] = (
    NationalIdCountryPrefixedRule(),
)

DATE_RULES: tuple[Rule, ...] = (
    DatePlaceholderRule(),
)

# Sequential: the peer test sees the value AFTER the milli-euro tiers.
VALUE_RULES: tuple[Rule, ...] = (
    MilliEuroSiblingRatioRule(),
    MilliEuroCountryPriorRule(),
    ValuePeerOutlierRule(),
)

ALL_RULES: tuple[Rule, ...] = (
    NAME_RULES + IDENTIFIER_RULES + DATE_RULES + VALUE_RULES
)

__all__ = [
    "Rule", "NAME_RULES", "IDENTIFIER_RULES", "DATE_RULES", "VALUE_RULES",
    "ALL_RULES",
]
