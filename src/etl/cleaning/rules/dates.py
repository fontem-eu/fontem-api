"""The date sanity the loader always applied, as a counted rule.

TED and the national gateways write ``2000-01-01`` / ``1900-01-01``
where they mean "unknown". The loader drops them from the typed date
fields (behaviour unchanged — ``_as_day`` calls ``is_placeholder_day``);
this rule reports every raw date field that carried one, so the
placeholder is counted per notice, per country and per year.
"""
from __future__ import annotations

from typing import Sequence

from ..facts import NoticeFacts
from ..lookups import Lookups
from ..outcomes import DatePlaceholder, Outcome

PLACEHOLDER_DAYS = ("2000-01-01", "1900-01-01")

DATE_FIELDS = (
    "award_date_raw",
    "tender_result_award_date_raw",
    "publication_date_raw",
    "issue_date_raw",
)


def is_placeholder_day(raw) -> bool:
    """True for a sentinel date (with or without a timezone suffix)."""
    return isinstance(raw, str) and raw.startswith(PLACEHOLDER_DAYS)


class DatePlaceholderRule:
    """``generic.date_placeholder``: one outcome per sentinel field."""

    id = "generic.date_placeholder"

    def applies(self, facts: NoticeFacts, lookups: Lookups) -> bool:  # pylint: disable=unused-argument
        return any(getattr(facts, f) for f in DATE_FIELDS)

    def decide(self, facts: NoticeFacts, lookups: Lookups) -> Sequence[Outcome]:  # pylint: disable=unused-argument
        return [
            DatePlaceholder(rule_id=self.id, subject=f, raw=getattr(facts, f))
            for f in DATE_FIELDS if is_placeholder_day(getattr(facts, f))
        ]
