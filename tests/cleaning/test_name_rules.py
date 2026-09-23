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
    NameIsPlaceholderRule, NotApplicableRule, SeeTheAnnexRule,
    SeveralOperatorsRule, has_legal_form, vouched_by_legal_form,
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
        "giusta determinazione dirigenziale n. 1287 del 2019",
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


# Real names the deleted function-word heuristic withheld in prod. It
# hit 5.2% of every name longer than 80 characters; these are the kind
# of institution that reads as a sentence because that is what it is
# called.
REAL_NAMES_THE_HEURISTIC_KILLED = [
    ("ASSOCIATION DE FORMATION POUR LA COOPERATION ET LA PROMOTION "
     "PROFESSIONNELLE MEDITERRANEENNE"),
    ("INSTITUT NATIONAL DE FORMATION ET DE RECHERCHES SUR L'EDUCATION "
     "PERMANENTE INFREP"),
    ("THE SOCIETY FOR THE ORPHANS & CHILDREN OF MINISTERS AND MISSIONARIES "
     "OF THE PRESBYTERIAN CHURCH"),
    "SETTLEMENT FOR THE BENEFIT OF THE CHILDREN OF PETER JAMES ILLINGWORTH",
    ("FONDO PENSIONE COMPLEMENTARE A CONTRIBUZIONE DEFINITA E A "
     "CAPITALIZZAZIONE INDIVIDUALE PER I LAVORATORI"),
]


@pytest.mark.parametrize("name", REAL_NAMES_THE_HEURISTIC_KILLED)
def test_no_rule_withholds_a_real_institution(name):
    """The regression that motivated naming the families: withholding
    deletes a supplier from its award, and nothing downstream can
    recover the name."""
    for rule in (ItalianNoticeTextRule(), MultipleAwardeesRule(),
                 SeveralOperatorsRule(), SeeTheAnnexRule(),
                 NotApplicableRule(), NameContainsUrlRule(),
                 NameIsPlaceholderRule()):
        assert not _hits(rule, name), rule.id


class TestSeeTheAnnex:
    """The buyer points somewhere else instead of naming anyone."""

    @pytest.mark.parametrize("name", [
        "See attached",                                  # IRL
        "See attached excel file",                       # IRL
        "See attached Supplier List",                    # GBR
        "Lot 2 - voir liste section VI",                 # FRA
        "VOIR LISTE",                                    # FRA
        "Voir liste attributaires ci-dessous",           # FRA
        "Voir liste sur site Apetra",                    # BEL
        "zie bijlage",                                   # NLD
        "Zie bijlage 'Gegunde partijen Software werkplekken'",
        "Se bilaga",                                     # SWE
        "Ver anexo publicado en el perfil de contratante",   # ESP
        "Vedi allegato pubblicato nel seguente link",        # ITA
    ])
    def test_withholds_a_pointer_instead_of_a_name(self, name):
        assert _hits(SeeTheAnnexRule(), name) == ["ORG-1"]


class TestSeveralOperators:
    """The buyer writes how many won instead of who won."""

    @pytest.mark.parametrize("name", [
        "Various Suppliers",                    # GBR
        "Various suppliers over 7 categories",  # GBR
        "Multiple Suppliers",                   # GBR
        "Lot 1 - Multiple Suppliers",           # IRL
        "Framework of multiple suppliers",      # IRL
        "Plusieurs attributaires (accord-cadre)",   # BEL
        "marche conclu avec plusieurs attributaires",   # FRA
        "Mehrere Auftragnehmer (siehe Abschnitt VI.2)",  # DEU
        "Meerdere ondernemingen, zie bijlage A",    # NLD
        "diverse leveranciers",                     # NLD
        "Vari operatori",                           # ITA
        "Varie ditte (vedi allegato A.2)",          # ITA
    ])
    def test_withholds_a_plurality(self, name):
        assert _hits(SeveralOperatorsRule(), name) == ["ORG-1"]


class TestNotApplicable:
    """The buyer refuses the field, sometimes citing a statute."""

    @pytest.mark.parametrize("name", [
        "Not Applicable",                                     # GBR
        "Not Applicable — No Award Made",                     # GBR
        "Niet van toepassing.",                               # NLD
        "nie dotyczy",                                        # POL
        "Keine Angabe",                                       # AUT
        "Keine Angabe aus Gründen des Wettbewerbs",           # DEU/AUT
        "Keine Angabe gemäß § 61 Abs. 4 BVergG 2018",         # AUT
    ])
    def test_withholds_a_refusal(self, name):
        assert _hits(NotApplicableRule(), name) == ["ORG-1"]


class TestTheLegalFormGuard:
    def test_a_real_company_the_buyer_annotated_is_spared(self):
        """"Lange Deele BV (zie bijlage)" is a supplier plus a note, not
        a note. The legal form sits before the bracket, so the guard has
        to look past the annotation."""
        assert vouched_by_legal_form("Lange Deele BV (zie bijlage)")
        assert not _hits(SeeTheAnnexRule(), "Lange Deele BV (zie bijlage)")
        assert not _hits(SeeTheAnnexRule(), "Acme Solutions Ltd (see attached)")

    @pytest.mark.parametrize("name", [
        "Varie ditte (vedi allegato A.2)",
        "RV-Partner 3 (Keine Angabe aus Gründen des Wettbewerbs)",
        "Diverse locaties (zie bijlage)",
    ])
    def test_junk_in_front_of_the_bracket_is_still_junk(self, name):
        assert not vouched_by_legal_form(name)


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
