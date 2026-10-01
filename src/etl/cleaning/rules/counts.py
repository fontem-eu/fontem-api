"""Bidder counts: the received-tenders figure a buyer typed (gitops#480).

The figure drives the single-bidder indicator and every average of
competition, and buyers put other things in it. Measured on fontem-prod,
2026-10-01, by reading the notices behind every count above 10,000:

* money or a reference: 2,416,436 (148462-2026, IRL), 800,000
  (541842-2024, FRA), 629,812, 325,350, and 93,040 against a VALUE of
  96,040 (120673-2011, GBR);
* keyboard runs: 12,345 and 11,111;
* a filler: exactly 999 on 1,498 notices, against 2 on 998 and 22 on
  996 (mostly DEU, 2016-2026).

The genuine large counts are dynamic purchasing systems and transport
frameworks, and their own breakdowns add up: 6,827 (FynBus FV9, 1,168
tenders listed), 1,617 (Slovenian state forests: 1,103 unverified + 514
inadequate), 1,164 (Austrian forestry, quarterly DPS awards), 1,034. So
the line is drawn above them, not at a round number below.

Some notices publish a sane total beside the bad one (154038-2026:
t-esubm 325,350, then t-esubm 3; 776313-2025: tenders 67,494, t-esubm 1).
The parser hands every total over in its choice order; the rule falls
back to the first usable one, and withholds the count only when there is
none. Either way the published figure stays on the event
(``tenders_received_raw``).

Not here, on purpose: 99 (463 notices against ~118 on its neighbours, so
about a quarter are real) and the 940 a Greek agency printed on each of
its ~11,600 childcare-voucher awards of 2014-16 (the programme's total,
published as such). Both are plausible counts; neither is decidable
from the notice.
"""
from __future__ import annotations

from typing import Sequence

from ..facts import NoticeFacts
from ..lookups import Lookups
from ..outcomes import BidderCountRejected, Outcome

# Above every genuine count found (6,827) and below every bad one
# (11,111). No procurement draws ten thousand tenders.
MAX_PLAUSIBLE_TENDERS = 10_000
# "Many" or "not counted", typed as the largest figure the field seemed to take.
PLACEHOLDER_COUNTS = frozenset({999, 9_999})

SUBJECT = "tenders_received"


def usable(count: int) -> bool:
    """A count the platform can stand behind."""
    return 0 < count <= MAX_PLAUSIBLE_TENDERS and count not in PLACEHOLDER_COUNTS


def _fallback(totals: tuple[int, ...]) -> int | None:
    return next((n for n in totals[1:] if usable(n)), None)


class _BidderCountRule:
    id = ""

    def rejects(self, count: int) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def applies(self, facts: NoticeFacts, lookups: Lookups) -> bool:  # pylint: disable=unused-argument
        return bool(facts.bidder_totals) and self.rejects(facts.bidder_totals[0])

    def decide(self, facts: NoticeFacts, lookups: Lookups) -> Sequence[Outcome]:  # pylint: disable=unused-argument
        totals = facts.bidder_totals
        return [BidderCountRejected(
            rule_id=self.id, subject=SUBJECT, raw=totals[0], kept=_fallback(totals))]


class ImpossibleBidderCountRule(_BidderCountRule):
    """More tenders than any procurement draws: money, a reference or a
    keyboard run typed into the count."""

    id = "generic.bidder_count_impossible"

    def rejects(self, count: int) -> bool:
        return count > MAX_PLAUSIBLE_TENDERS


class PlaceholderBidderCountRule(_BidderCountRule):
    """999 (or 9,999): a filler for "many" or "not counted"."""

    id = "generic.bidder_count_placeholder"

    def rejects(self, count: int) -> bool:
        return count in PLACEHOLDER_COUNTS
