"""C4 - monetary scale.

The milli-euro tiers rescale (they can prove the factor); the peer test
only quarantines, because nothing in the XML marks the scale and the
correction is then undecidable (verified 2026-09-21: "no decimals" does
not imply cents).
"""
import pytest

from src.etl.cleaning.lookups import InMemoryPeerStats, Lookups, PeerBand
from src.etl.cleaning.outcomes import Quarantine, Rescale
from src.etl.cleaning.rules.values import (
    PEER_K_DEFAULT, PEER_K_WITH_PLACEHOLDER, PEER_MIN_N,
    REASON_AMBIGUOUS_SCALE, REASON_IMPLAUSIBLE_VS_PEERS,
    MilliEuroCountryPriorRule, MilliEuroSiblingRatioRule, ValuePeerOutlierRule,
)
from src.etl.cleaning.stage import run_stage

from .factories import facts, org, value

# TED 646890-2026: a EUR 24,474,133 network switch contract in Beja,
# CPV 32420000. Its PT/3242 peers are five-figure contracts.
BEJA_EUR = 24474133.0
BEJA = {"country": "PRT", "cpv": "32420000"}
PT_SWITCHES = PeerBand(n=412, p10=8_000.0, median=45_000.0, p90=180_000.0,
                       p99=900_000.0)


def _stats(band=PT_SWITCHES, key=("PRT", "3242")):
    return Lookups(peer_stats=InMemoryPeerStats({key: band}))


def _decide(v, lookups, **notice_kwargs):
    return ValuePeerOutlierRule().decide(facts(org("A"), value=v, **notice_kwargs),
                                         lookups)


class TestPeerOutlier:
    def test_quarantines_the_beja_value_as_ambiguous(self):
        [out] = _decide(value(BEJA_EUR, **BEJA), _stats())
        assert out.reason == REASON_AMBIGUOUS_SCALE
        assert out.value_eur == BEJA_EUR
        assert out.candidates == (BEJA_EUR / 100, BEJA_EUR / 1000)
        # /100 lands inside the peers' band, which is what makes the
        # correction ambiguous rather than impossible.
        assert PT_SWITCHES.p10 <= BEJA_EUR / 100 <= PT_SWITCHES.p99
        assert "in band" in out.detail

    def test_an_outlier_no_division_can_explain_is_merely_implausible(self):
        [out] = _decide(value(9e12, **BEJA), _stats())
        assert out.reason == REASON_IMPLAUSIBLE_VS_PEERS
        assert "in band" not in out.detail

    def test_a_value_within_the_bar_is_kept(self):
        assert not _decide(value(PEER_K_DEFAULT * PT_SWITCHES.p90, **BEJA), _stats())

    def test_the_gateway_watermark_lowers_the_bar(self):
        just_over_20x = PEER_K_WITH_PLACEHOLDER * PT_SWITCHES.p90 + 1
        assert not _decide(value(just_over_20x, **BEJA), _stats())
        [out] = _decide(value(just_over_20x, **BEJA), _stats(),
                        tender_result_award_date_raw="2000-01-01+01:00")
        assert out.rule_id == "generic.value_peer_outlier"
        assert "gateway placeholder present" in out.detail

    def test_the_tender_reference_watermark_counts_too(self):
        just_over_20x = PEER_K_WITH_PLACEHOLDER * PT_SWITCHES.p90 + 1
        assert _decide(value(just_over_20x, **BEJA), _stats(),
                       tender_reference="0.0")

    def test_a_small_peer_group_is_not_evidence(self):
        thin = PeerBand(n=PEER_MIN_N - 1, p10=1.0, median=2.0, p90=3.0, p99=4.0)
        assert not _decide(value(BEJA_EUR, **BEJA), _stats(thin))

    def test_an_unknown_peer_group_is_not_evidence(self):
        assert not _decide(value(BEJA_EUR, country="PRT", cpv="09000000"),
                       _stats())

    def test_the_rule_is_inactive_without_injected_stats(self):
        notice = facts(org("A"), value=value(BEJA_EUR, **BEJA))
        assert not ValuePeerOutlierRule().applies(notice, Lookups())

    @pytest.mark.parametrize("v", [
        value(None, **BEJA), value(BEJA_EUR, country=None, cpv="32420000"),
        value(BEJA_EUR, country="PRT", cpv=None),
    ])
    def test_the_rule_needs_a_value_a_country_and_a_cpv(self, v):
        assert not ValuePeerOutlierRule().applies(facts(org("A"), value=v), _stats())


class TestMilliEuroTiers:
    def test_the_sibling_ratio_tier_rescales(self):
        v = value(4250000.0, estimate_eur=4250.0, payable_eur=4250000.0,
                  total_original=4250000.0, payable_original=4250000.0,
                  country="PRT")
        [out] = MilliEuroSiblingRatioRule().decide(facts(org("A"), value=v), Lookups())
        assert out.tier == "ratio"
        assert out.factor == pytest.approx(0.001)
        assert out.corrected.total_eur == 4250.0

    def test_the_country_prior_tier_rescales(self):
        v = value(2.5e9, payable_eur=2.5e9, total_original=2.5e9,
                  payable_original=2.5e9, country="PRT")
        [out] = MilliEuroCountryPriorRule().decide(facts(org("A"), value=v), Lookups())
        assert out.tier == "country_prior"
        assert out.corrected.total_eur == 2.5e6

    def test_each_tier_claims_only_its_own_correction(self):
        v = value(2.5e9, payable_eur=2.5e9, total_original=2.5e9,
                  payable_original=2.5e9, country="PRT")
        notice = facts(org("A"), value=v)
        assert not MilliEuroSiblingRatioRule().decide(notice, Lookups())

    def test_a_plausible_value_is_untouched(self):
        v = value(1200.0, estimate_eur=1000.0, payable_eur=1200.0,
                  total_original=1200.0, payable_original=1200.0, country="FRA")
        notice = facts(org("A"), value=v)
        assert not MilliEuroSiblingRatioRule().decide(notice, Lookups())
        assert not MilliEuroCountryPriorRule().decide(notice, Lookups())


class TestValueRulesInSequence:
    def test_the_peer_test_sees_the_value_the_tiers_corrected(self):
        """A PRT notice at EUR 9.5bn: tier B divides it to 9.5M, and the
        peer test then judges 9.5M - not 9.5bn - against the band. The
        quarantine candidates prove which value was judged: they are
        derived from the corrected figure."""
        v = value(9.5e9, payable_eur=9.5e9, total_original=9.5e9,
                  payable_original=9.5e9, country="PRT", cpv="32420000")
        result = run_stage(facts(org("A"), value=v), _stats())
        assert result.rules_fired == ("pt.value_scale_country_prior",
                                      "generic.value_peer_outlier")
        assert isinstance(result.value, Quarantine)
        assert result.value.candidates == (9.5e6 / 100, 9.5e6 / 1000)

    def test_a_rescale_that_lands_among_its_peers_raises_no_objection(self):
        """The same notice one order of magnitude lower: tier B still
        corrects it, and 2.5M is inside 50 x p90, so the value stands."""
        v = value(2.5e9, payable_eur=2.5e9, total_original=2.5e9,
                  payable_original=2.5e9, country="PRT", cpv="32420000")
        result = run_stage(facts(org("A"), value=v), _stats())
        assert result.rules_fired == ("pt.value_scale_country_prior",)
        assert isinstance(result.value, Rescale)

    def test_a_rescale_with_no_peer_objection_stays_a_rescale(self):
        v = value(2.5e9, payable_eur=2.5e9, total_original=2.5e9,
                  payable_original=2.5e9, country="PRT", cpv="32420000")
        roomy = PeerBand(n=99, p10=1e5, median=1e6, p90=1e6, p99=1e7)
        result = run_stage(facts(org("A"), value=v), _stats(roomy))
        assert isinstance(result.value, Rescale)
        assert result.value.reason == "country_prior"
        assert result.value.corrected.total_eur == 2.5e6

    def test_a_quarantine_is_the_final_word(self):
        v = value(BEJA_EUR, **BEJA)
        result = run_stage(facts(org("A"), value=v), _stats())
        assert isinstance(result.value, Quarantine)
        assert result.value.reason == REASON_AMBIGUOUS_SCALE
