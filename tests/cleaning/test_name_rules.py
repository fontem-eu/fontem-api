"""C2 name rules.

Every "junk" string is one a buyer really published, and every "real"
string is a company that really exists in the graph — the false
positives are the point: withholding a supplier deletes it from the
award, so a rule that fires on a real name loses data that no later
stage can recover.
"""
import pytest

from src.etl.cleaning.rules.names import (
    ItalianNoticeTextRule, MultipleAwardeesRule, NameContainsUrlRule,
    NameIsPlaceholderRule, NameIsSentenceRule, function_word_ratio,
    has_legal_form, is_sentence,
)

from .factories import buyer, facts, org

# The notice this rule family was written for: TED 184512-2013, where
# the whole award decree sits inside <OFFICIALNAME> and nothing else in
# the document names the operator.
ITALIAN_JUNK = (
    "Gara aggiudicata come da determina n. 543 del 2013 pubblicata sul "
    "sito www.csc.sanita.fvg.it alla sezione delibere e decreti."
)

# Real companies whose names contain "vedi" as a substring. The plan's
# bare "vedi " pattern matched 116 of these in prod (2026-09-22).
VEDI_REAL_COMPANIES = [
    "TRIVEDI NOMINEES PTY LTD",
    "OPTIMUM FUND - CSOB BYCI A MEDVEDI PLUS 1",
    "VEDI TOURGROUP s.r.o.",
    "Domus Arvedi Stiftung",
    "Invedi GmbH",
    "MARAVEDI CORPORACION SOCIEDAD LIMITADA",
    "ProVedi OU",
    "BAN, S. SZABO & PARTNERS UGYVEDI IRODA",
    "CHATURVEDI MOTORS PRIVATE LIMITED",
    "DWIVEDI ASSOCIATES PRIVATE LIMITED",
]


def _hits(rule, name, **facts_kwargs):
    """Run one rule over a notice with a single supplier."""
    result = rule.decide(facts(org(name), **facts_kwargs), None)
    return [o.subject for o in result]


class TestItalianNoticeText:
    def test_withholds_the_decree_boilerplate(self):
        rule = ItalianNoticeTextRule()
        assert _hits(rule, ITALIAN_JUNK) == ["ORG-1"]

    @pytest.mark.parametrize("name", [
        "Gara aggiudicata a seguito di procedura aperta",
        "Come da determina dirigenziale",
        "determina n. 543",
        "Aggiudicato con delibera del direttore generale",
        "Si veda la sezione delibere e decreti",
        "Pubblicata sul sito istituzionale dell'ente",
    ])
    def test_withholds_each_phrase_of_the_family(self, name):
        assert _hits(ItalianNoticeTextRule(), name) == ["ORG-1"]

    @pytest.mark.parametrize("name", VEDI_REAL_COMPANIES)
    def test_spares_real_companies_containing_vedi(self, name):
        """The regression the bare "vedi " pattern would have caused."""
        assert _hits(ItalianNoticeTextRule(), name) == []

    @pytest.mark.parametrize("name", [
        "Lotto Nord S.r.l.",          # "lotto" without a number
        "Determinazione Group SA",    # not "determina n. <digits>"
    ])
    def test_spares_names_that_merely_resemble_the_phrases(self, name):
        assert _hits(ItalianNoticeTextRule(), name) == []


class TestNameContainsUrl:
    @pytest.mark.parametrize("name", [
        "https://contractaciopublica.cat/ca/detall-publicacio/300750688",
        "DIFERENTES ADJUDICATARIOS (https://contractaciopublica.cat/x/300670279)",
        "See: Https://staffordshire.gov.uk/business/procurement/DPS-Award-Notices",
        "NORABIO ET AUTRES CANDIDATS RETENUS / https://filesender.renater.fr/?s=x",
        ITALIAN_JUNK,
    ])
    def test_withholds_a_pointer_to_a_page(self, name):
        assert _hits(NameContainsUrlRule(), name) == ["ORG-1"]

    @pytest.mark.parametrize("name", [
        "WWW.KOTVA.CZ, a.s.",
        "www.levnedobijeni.cz s.r.o.",
        "www.shipments.de Wordemann GmbH",
        "www.heymountain.com GmbH",
        "WWW.IDEANDERSEN.DK ApS",
        "WWW.THEARTISANCO.CO.UK LIMITED",
        "www.mtalo Oy",
        "HTTPS OU",                     # a real Estonian company
        "WWW.BRETAGNEPATRIMOINE.COM",   # a brand, not a sentence
    ])
    def test_spares_companies_trading_under_their_domain(self, name):
        assert _hits(NameContainsUrlRule(), name) == []


class TestMultipleAwardees:
    @pytest.mark.parametrize("name", [
        "Varios adjudicatarios",
        "Varios adjudicatarios (ver resolucion adjudicacion)",
        "DIFERENTES ADJUDICATARIOS",
        "DIVERSOS HOMOLOGADOS",
        "DIVERSOS HOMOLOGATS - https://contractaciopublica.cat/x",
        "distintos adjudicatarios del acuerdo marco",
    ])
    def test_withholds_the_several_winners_placeholder(self, name):
        assert _hits(MultipleAwardeesRule(), name) == ["ORG-1"]

    def test_spares_a_company_whose_name_shares_the_root(self):
        assert _hits(MultipleAwardeesRule(), "Adjudicataria Iberica S.L.") == []


class TestNameIsPlaceholder:
    @pytest.mark.parametrize("name", [
        "n/a", "N/A", " na ", "Nao Aplicavel", "varios", "VARIOS",
        "diversi", "-", ".", "1", "0", "x", "XXX",
    ])
    def test_withholds_a_placeholder(self, name):
        assert _hits(NameIsPlaceholderRule(), name) == ["ORG-1"]

    @pytest.mark.parametrize("name", ["NA Group Ltd", "X-Rail GmbH", "Diversified SA"])
    def test_spares_a_name_that_merely_starts_with_one(self, name):
        assert _hits(NameIsPlaceholderRule(), name) == []


class TestNameIsSentence:
    def test_withholds_the_decree_sentence(self):
        assert _hits(NameIsSentenceRule(), ITALIAN_JUNK,
                     notice_language="ITA") == ["ORG-1"]

    def test_a_short_name_is_never_a_sentence(self):
        assert not is_sentence("Visualforma - Tecnologias de Informacao, S.A.", "POR")

    def test_a_long_name_without_sentence_shape_is_kept(self):
        long_name = (
            "Consorzio Nazionale Servizi Societa Cooperativa per Azioni "
            "Divisione Facility Management Area Nord Est Italia"
        )
        assert not is_sentence(long_name, "ITA")

    def test_a_decree_clause_alone_is_enough(self):
        name = ("Affidamento del servizio di pulizia giusta determinazione "
                "dirigenziale n. 1287 del 2019 esecutiva dal quindici marzo")
        assert is_sentence(name, "ITA")

    def test_function_words_alone_are_enough(self):
        name = ("il servizio e stato affidato alla ditta che ha presentato "
                "la migliore offerta per il lotto unico della procedura")
        assert function_word_ratio(name, "ITA") >= 0.35
        assert is_sentence(name, "ITA")

    def test_a_legal_form_exempts_a_long_name_from_this_rule_only(self):
        name = ("Aggiudicataria del servizio di ristorazione per le scuole "
                "della citta con la sua rete di cucine S.p.A.")
        assert has_legal_form(name)
        assert not is_sentence(name, "ITA")
        # ... but the Italian boilerplate still withholds it.
        assert _hits(ItalianNoticeTextRule(),
                     "Gara aggiudicata alla " + name) == ["ORG-1"]

    def test_the_language_selects_the_word_list(self):
        portuguese = ("o servico foi adjudicado a empresa que apresentou a "
                      "proposta mais vantajosa para o lote unico do concurso")
        assert function_word_ratio(portuguese, "POR") > function_word_ratio(
            portuguese, "DEU")

    def test_an_unknown_language_uses_every_list(self):
        portuguese = ("o servico foi adjudicado a empresa que apresentou a "
                      "proposta mais vantajosa para o lote unico do concurso")
        assert is_sentence(portuguese, None)


class TestHasLegalForm:
    @pytest.mark.parametrize("name", [
        "Visualforma - Tecnologias de Informacao, S.A.",
        "Siemens AG", "Vattenfall AB", "Nokia Oyj", "Telia Company AB",
        "Przedsiebiorstwo Sp. z o.o.", "Skanska A/S", "Heidelberg GmbH & Co. KG",
        "Bouygues SAS", "Acme Kft", "Domov s.r.o.", "ProVedi OU",
    ])
    def test_recognises_a_legal_form(self, name):
        assert has_legal_form(name)

    @pytest.mark.parametrize("name", [
        "Gara aggiudicata come da determina", "Varios adjudicatarios",
        "Ministerio da Saude", "se veda la determina",
    ])
    def test_does_not_invent_one(self, name):
        assert not has_legal_form(name)

    def test_a_bare_two_letter_word_is_not_a_legal_form(self):
        """"se" and "as" are function words in several languages."""
        assert not has_legal_form("se as obras forem concluidas")


class TestRuleGuards:
    def test_a_notice_without_named_suppliers_skips_the_name_rules(self):
        unnamed = facts(org(None))
        assert not ItalianNoticeTextRule().applies(unnamed, None)

    def test_the_buyer_is_never_withheld_by_a_name_rule(self):
        notice = facts(buyer(ITALIAN_JUNK), org("Real Supplier S.p.A."))
        assert ItalianNoticeTextRule().decide(notice, None) == []
