"""The two TED notices the rules were written from, end to end:
real XML -> parser -> adapter -> stage.
"""
import pathlib

import pytest
from eforms.parser import parse as parse_notice_xml

from src.etl.cleaning.adapter import facts_from_notice
from src.etl.cleaning.facts import ValueFacts
from src.etl.cleaning.lookups import InMemoryPeerStats, Lookups, PeerBand
from src.etl.cleaning.outcomes import Quarantine
from src.etl.cleaning.stage import run_stage

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "ted"


def _notice(name):
    return parse_notice_xml((FIXTURES / name).read_bytes())


class TestBeja646890:
    """A PRT award: bare NIFs and an integer EUR 24,474,133 for network
    switches (CPV 32420000)."""

    @pytest.fixture()
    def facts_(self):
        notice = _notice("646890-2026.xml")
        award = notice.awards[0]
        return facts_from_notice(
            notice,
            value=ValueFacts(total_eur=award.value, payable_eur=award.value,
                             country="PRT", cpv=notice.cpv_main),
            context_award=award,
        )

    def test_the_adapter_reads_the_published_identifiers(self, facts_):
        by_id = {o.org_id: o for o in facts_.organizations}
        assert by_id["ORG-0002"].name.startswith("Visualforma")
        assert by_id["ORG-0002"].legal_id == "503536717"
        assert by_id["ORG-0002"].legal_scheme is None
        assert facts_.notice_language == "POR"

    def test_the_bare_nifs_become_vats(self, facts_):
        result = run_stage(facts_)
        assert result.identifiers["ORG-0002"] == "PT503536717"
        assert result.identifiers["ORG-0001"] == "PT504884620"
        assert "generic.national_id_country_prefixed" in result.rules_fired

    def test_no_supplier_is_withheld(self, facts_):
        """Visualforma is a real company; nothing about this notice's
        names is junk."""
        assert not run_stage(facts_).withheld

    def test_the_value_is_quarantined_against_its_peers(self, facts_):
        stats = Lookups(peer_stats=InMemoryPeerStats(
            {("PRT", "3242"): PeerBand(n=412, p10=8e3, median=4.5e4,
                                       p90=1.8e5, p99=9e5)}))
        result = run_stage(facts_, stats)
        assert isinstance(result.value, Quarantine)
        assert result.value.reason == "ambiguous_scale_x100_or_x1000"
        assert result.value.candidates[0] == pytest.approx(244741.33)


class TestUdine184512:
    """A legacy TED_EXPORT notice whose <OFFICIALNAME> is the award
    decree, not a name."""

    @pytest.fixture()
    def facts_(self):
        notice = _notice("184512-2013.xml")
        award = notice.awards[0]
        return facts_from_notice(
            notice,
            value=ValueFacts(total_eur=award.value, payable_eur=award.value,
                             country="ITA", cpv=notice.cpv_main),
            context_award=award,
        )

    def test_the_supplier_is_withheld_with_its_raw_text(self, facts_):
        result = run_stage(facts_)
        assert result.withheld == {
            "contractor-0": "it.notice_text_in_supplier_name"}
        [withheld] = [o for o in result.outcomes
                      if o.rule_id == "it.notice_text_in_supplier_name"]
        assert withheld.name_raw.startswith("Gara aggiudicata come da determina")
        assert withheld.role == "winner"

    def test_the_buyer_survives(self, facts_):
        """Only the supplier's name is junk; withholding must not touch
        the authority."""
        result = run_stage(facts_)
        assert "buyer" not in result.withheld

    def test_the_legacy_notice_carries_a_two_letter_language(self, facts_):
        assert facts_.notice_language == "IT"


class TestBidderCounts:
    """gitops#480: the published count, as the stage reads it, on the
    real notices behind the prod outliers (and one genuine large count)."""

    @staticmethod
    def _read(name):
        notice = _notice(name)
        award = next((a for a in notice.awards if a.is_winner), notice.awards[0])
        result = run_stage(facts_from_notice(notice, value=ValueFacts(), context_award=award))
        return result.tenders_received, result.rules_fired

    def test_money_in_the_count_is_withheld(self):
        # 148462-2026 (IRL): "tenders" 2,416,436 and nothing else.
        count, fired = self._read("148462-2026.xml")
        assert count is None
        assert "generic.bidder_count_impossible" in fired

    def test_the_lots_second_total_replaces_the_first(self):
        # 154038-2026 (FRA): t-esubm 325,350, then t-esubm 3.
        assert self._read("154038-2026.xml")[0] == 3

    def test_the_lots_own_electronic_count_contradicts_the_total(self):
        # 776313-2025 (SVN): tenders 67,494, t-esubm 1.
        assert self._read("776313-2025.xml")[0] == 1

    def test_the_999_filler_is_withheld(self):
        count, fired = self._read("556267-2024.xml")
        assert count is None
        assert "generic.bidder_count_placeholder" in fired

    def test_a_genuine_dynamic_purchasing_system_keeps_its_count(self):
        # 381964-2026 (SVN state forests): 1,617 = 1,103 unverified + 514
        # inadequate; nothing fires.
        count, fired = self._read("381964-2026.xml")
        assert count == 1617
        assert not [r for r in fired if "bidder" in r]
