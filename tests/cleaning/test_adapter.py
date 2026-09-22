"""The adapter: the only place that knows the parser's object shape.

It is deliberately defensive - an older wheel, a missing element or a
test double must become None rather than a surprise inside a rule.
"""
from types import SimpleNamespace

import pytest

from src.etl.cleaning.adapter import (
    BUYER_FALLBACK_ORG_ID, facts_from_notice, integer, number, org_facts, text,
)
from src.etl.cleaning.facts import ROLE_NAMED_TENDERER, ROLE_WINNER, ValueFacts


class TestCoercion:
    @pytest.mark.parametrize("given,expected", [
        ("Alfa", "Alfa"), ("", None), (None, None), (7, None), (object(), None),
    ])
    def test_text(self, given, expected):
        assert text(given) == expected

    @pytest.mark.parametrize("given,expected", [
        (7, 7.0), (7.5, 7.5), (True, None), (None, None), ("7", None),
    ])
    def test_number(self, given, expected):
        assert number(given) == expected

    @pytest.mark.parametrize("given,expected", [
        (7, 7), (0, 0), (-1, None), (True, None), (7.0, None), ("7", None),
    ])
    def test_integer(self, given, expected):
        assert integer(given) == expected


def _org(name="Alfa S.p.A.", country="ITA", legal_id="IT00123456789",
         scheme="VAT"):
    legal = SimpleNamespace(value=legal_id, scheme_name=scheme)
    return SimpleNamespace(name=name, country=country, legal_id=legal)


class TestOrgFacts:
    def test_reads_the_published_fields(self):
        got = org_facts(_org(), "ORG-1", ROLE_WINNER)
        assert (got.name, got.country, got.legal_id, got.legal_scheme) == (
            "Alfa S.p.A.", "ITA", "IT00123456789", "VAT")

    def test_an_organisation_without_a_legal_id_object(self):
        got = org_facts(SimpleNamespace(name="Alfa", country="ITA"),
                        "ORG-1", ROLE_WINNER)
        assert got.legal_id is None and got.legal_scheme is None


def _notice(*, awards, organizations, buyer=_org("Comune", "ITA", "IT9", None),
            **kwargs):
    return SimpleNamespace(
        publication_number=kwargs.pop("publication_number", "295342-2026"),
        notice_id="uuid-1", awards=awards, organizations=organizations,
        buyer=lambda: buyer, **kwargs)


def _award(org_id, *, is_winner=True, **kwargs):
    return SimpleNamespace(contractor_org_id=org_id, is_winner=is_winner, **kwargs)


class TestFactsFromNotice:
    def test_roles_and_the_buyer_fallback_id(self):
        notice = _notice(awards=[_award("O1"), _award("O2", is_winner=False)],
                         organizations={"O1": _org(), "O2": _org("Beta Srl")})
        facts = facts_from_notice(notice, value=ValueFacts())
        roles = {o.org_id: o.role for o in facts.organizations}
        assert roles["O1"] == ROLE_WINNER
        assert roles["O2"] == ROLE_NAMED_TENDERER
        assert BUYER_FALLBACK_ORG_ID in roles

    def test_a_supplier_that_won_one_lot_and_lost_another_is_a_winner(self):
        notice = _notice(
            awards=[_award("O1", is_winner=False), _award("O1")],
            organizations={"O1": _org()})
        facts = facts_from_notice(notice, value=ValueFacts())
        assert facts.suppliers[0].role == ROLE_WINNER

    def test_an_award_naming_an_unknown_organisation_is_skipped(self):
        notice = _notice(awards=[_award("MISSING")], organizations={})
        assert not facts_from_notice(notice, value=ValueFacts()).suppliers

    def test_raw_signals_travel_from_the_notice_and_the_context_award(self):
        notice = _notice(awards=[_award("O1")], organizations={"O1": _org()},
                         notice_language="POR",
                         tender_result_award_date_raw="2000-01-01+01:00",
                         publication_date="2026-09-04", issue_date="2026-09-01")
        award = SimpleNamespace(award_date_raw="2000-01-01", tender_reference="0.0")
        facts = facts_from_notice(notice, value=ValueFacts(), context_award=award)
        assert facts.notice_language == "POR"
        assert facts.award_date_raw == "2000-01-01"
        assert facts.tender_reference == "0.0"
        assert facts.has_gateway_placeholder

    def test_an_older_parser_without_the_raw_fields_yields_none(self):
        notice = _notice(awards=[_award("O1")], organizations={"O1": _org()})
        facts = facts_from_notice(notice, value=ValueFacts())
        assert facts.award_date_raw is None
        assert facts.tender_result_award_date_raw is None
        assert not facts.has_gateway_placeholder

    def test_a_notice_without_a_buyer(self):
        notice = _notice(awards=[], organizations={}, buyer=None)
        assert not facts_from_notice(notice, value=ValueFacts()).organizations

    def test_the_notice_id_falls_back_to_the_uuid(self):
        notice = _notice(awards=[], organizations={}, publication_number=None)
        assert facts_from_notice(notice, value=ValueFacts()).notice_id == "uuid-1"
