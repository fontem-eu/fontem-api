"""C3 - bare national identifiers.

The one rule that runs before identity assignment, because the matcher
needs the identifier. Everything here is deterministic: prefixing a NIF
with its country is not a guess, and the per-country regex inside
``canon_vat`` is what refuses everything that is not a VAT.
"""
import pytest

from src.etl.cleaning.lookups import Lookups, vat_prefix_for
from src.etl.cleaning.rules.identifiers import (
    NationalIdCountryPrefixedRule, base_identifier, prefixed_identifier,
)
from src.etl.cleaning.stage import run_stage

from .factories import buyer, facts, org

RULE = NationalIdCountryPrefixedRule()
LOOKUPS = Lookups()

# TED 646890-2026 (Beja, PRT): the winner's cbc:CompanyID is the bare
# NIF, with no schemeName. 42,573 of 43,045 PT companies had no VAT
# because of this (2026-09-21).
BEJA_NIF = "503536717"
# A German Leitweg-ID: a routing id, not a VAT. Prefixing it must not
# manufacture one.
LEITWEG_ID = "053660036036-31001-86"


def _decide(*organizations, lookups=LOOKUPS):
    return {o.subject: o.canonical
            for o in RULE.decide(facts(*organizations), lookups)}


class TestPrefixing:
    def test_a_bare_portuguese_nif_becomes_a_vat(self):
        supplier = org("Visualforma", country="PRT", legal_id=BEJA_NIF)
        assert _decide(supplier) == {"ORG-1": "PT503536717"}

    def test_an_already_canonical_vat_is_left_alone(self):
        supplier = org("Visualforma", country="PRT", legal_id="PT503536717")
        assert base_identifier(supplier) == "PT503536717"
        assert not _decide(supplier)

    def test_a_leitweg_id_stays_unidentified(self):
        supplier = org("Bundesdruckerei", country="DEU", legal_id=LEITWEG_ID)
        assert base_identifier(supplier) is None
        assert prefixed_identifier(supplier, LOOKUPS) is None
        assert not _decide(supplier)

    def test_alpha_2_and_alpha_3_are_both_accepted(self):
        assert _decide(org("A", country="PT", legal_id=BEJA_NIF)) == {
            "ORG-1": "PT503536717"}
        assert _decide(org("A", country="PRT", legal_id=BEJA_NIF)) == {
            "ORG-1": "PT503536717"}

    def test_greece_is_prefixed_el_not_gr(self):
        assert vat_prefix_for("GRC") == "EL"
        assert vat_prefix_for("GR") == "EL"
        greek = org("Ergon", country="GRC", legal_id="094014201")
        assert _decide(greek) == {"ORG-1": "EL094014201"}

    def test_the_united_kingdom_alias(self):
        assert vat_prefix_for("UK") == "GB"

    @pytest.mark.parametrize("country", [None, "", "USA", "ZZ", "XYZW"])
    def test_a_country_outside_the_map_yields_nothing(self, country):
        assert vat_prefix_for(country) is None
        assert _decide(org("A", country=country, legal_id=BEJA_NIF)) == {}

    def test_surrounding_whitespace_is_ignored(self):
        assert _decide(org("A", country="PRT", legal_id=f"  {BEJA_NIF} ")) == {
            "ORG-1": "PT503536717"}


class TestSchemes:
    @pytest.mark.parametrize("scheme", [None, "", "VAT", "NATIONAL", "EORI",
                                        "national", "Vat"])
    def test_accepted_schemes(self, scheme):
        supplier = org("A", country="PRT", legal_id=BEJA_NIF, legal_scheme=scheme)
        assert _decide(supplier) == {"ORG-1": "PT503536717"}

    @pytest.mark.parametrize("scheme", ["ORGANIZATION", "LEI", "DUNS"])
    def test_other_schemes_are_left_to_their_own_meaning(self, scheme):
        supplier = org("A", country="PRT", legal_id=BEJA_NIF, legal_scheme=scheme)
        assert not _decide(supplier)


class TestAppliesToEveryParty:
    def test_the_buyer_is_normalised_too(self):
        notice = facts(
            buyer("Municipio de Beja", country="PRT", legal_id="600082849"),
            org("Visualforma", country="PRT", legal_id=BEJA_NIF),
        )
        got = {o.subject: o.canonical for o in RULE.decide(notice, LOOKUPS)}
        assert got == {"BUYER-1": "PT600082849", "ORG-1": "PT503536717"}

    def test_a_notice_without_legal_ids_skips_the_rule(self):
        assert not RULE.applies(facts(org("A", legal_id=None)), LOOKUPS)


class TestThroughTheStage:
    def test_the_result_carries_an_entry_for_every_organisation(self):
        result = run_stage(facts(
            buyer("Municipio de Beja", country="PRT", legal_id="600082849"),
            org("Visualforma", country="PRT", legal_id=BEJA_NIF),
            org("No Id Supplier", org_id="ORG-2", country="PRT"),
        ))
        assert result.identifiers == {
            "BUYER-1": "PT600082849",
            "ORG-1": "PT503536717",
            "ORG-2": None,
        }
        assert result.rules_fired == ("generic.national_id_country_prefixed",)
        assert result.counters["generic.national_id_country_prefixed"] == 2
