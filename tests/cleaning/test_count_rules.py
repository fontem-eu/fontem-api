"""The bidder-count rules (gitops#480), on facts alone.

The numbers are the ones read off real notices on 2026-10-01: buyers
type money (2,416,436), keyboard runs (12,345) and a 999 filler into the
received-tenders count, and a few publish a sane total beside the bad one.
"""
from src.etl.cleaning.rules.counts import (
    MAX_PLAUSIBLE_TENDERS, PLACEHOLDER_COUNTS, usable,
)
from src.etl.cleaning.stage import run_stage

from .factories import facts


def _count(*totals):
    result = run_stage(facts(bidder_totals=tuple(totals)))
    return result.tenders_received, [r for r in result.rules_fired if "bidder" in r]


def test_an_impossible_count_with_nothing_beside_it_is_withheld():
    assert _count(2_416_436) == (None, ["generic.bidder_count_impossible"])


def test_an_impossible_count_falls_back_to_the_lots_other_total():
    # 154038-2026: t-esubm 325,350, then t-esubm 3 on the same lot.
    assert _count(325_350, 3) == (3, ["generic.bidder_count_impossible"])


def test_the_fallback_skips_totals_that_are_no_better():
    assert _count(800_000, 999, 2) == (2, ["generic.bidder_count_impossible"])


def test_the_999_filler_is_withheld():
    assert _count(999) == (None, ["generic.bidder_count_placeholder"])


def test_a_placeholder_falls_back_too():
    assert _count(999, 4) == (4, ["generic.bidder_count_placeholder"])


def test_genuine_large_counts_are_kept():
    # FynBus FV9 (6,827) and the Slovenian forestry DPS (1,617): their
    # breakdowns add up; the line sits above them.
    assert _count(6_827) == (6_827, [])
    assert _count(1_617, 1_617) == (1_617, [])


def test_99_and_940_are_left_alone():
    # Plausible counts, undecidable from the notice (see counts.py).
    assert _count(99) == (99, [])
    assert _count(940) == (940, [])


def test_no_published_count_fires_nothing():
    assert _count() == (None, [])


def test_the_bounds():
    assert usable(MAX_PLAUSIBLE_TENDERS)
    assert not usable(MAX_PLAUSIBLE_TENDERS + 1)
    assert not any(usable(n) for n in PLACEHOLDER_COUNTS)
    assert not usable(0)


def test_the_rule_is_counted_and_reported():
    result = run_stage(facts(bidder_totals=(2_416_436,)))
    assert result.counters["generic.bidder_count_impossible"] == 1
    [outcome] = [o for o in result.outcomes if o.rule_id.startswith("generic.bidder")]
    assert outcome.example() == {
        "field": "tenders_received", "raw": 2_416_436, "decision": "withheld"}
