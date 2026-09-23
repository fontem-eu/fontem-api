"""How the stage folds many outcomes into one result."""
from src.etl.cleaning.lookups import InMemoryPeerStats, Lookups, PeerBand
from src.etl.cleaning.outcomes import Keep, Quarantine
from src.etl.cleaning.stage import run_stage

from .factories import buyer, facts, org, value

ITALIAN_JUNK = (
    "Gara aggiudicata come da determina n. 543 del 2013 pubblicata sul "
    "sito www.csc.sanita.fvg.it alla sezione delibere e decreti."
)


def test_a_clean_notice_fires_nothing():
    result = run_stage(facts(buyer("Comune di Trieste"), org("Alfa S.p.A.")))
    assert not result.fired
    assert not result.withheld
    assert isinstance(result.value, Keep)
    assert not result.counters


def test_a_supplier_hit_by_several_rules_is_withheld_under_the_first():
    """The junk sentence hits the Italian rule, the URL rule and the
    sentence rule. NAME_RULES order decides which one explains it."""
    result = run_stage(facts(org(ITALIAN_JUNK), notice_language="ITA"))
    assert result.withheld == {"ORG-1": "it.notice_text_in_supplier_name"}
    # every rule that saw it is still counted
    assert set(result.counters) == {
        "it.notice_text_in_supplier_name",
        "generic.name_contains_url",
    }


def test_rules_fired_is_ordered_and_deduplicated():
    result = run_stage(facts(
        org(ITALIAN_JUNK),
        org(ITALIAN_JUNK, org_id="ORG-2"),
        notice_language="ITA",
    ))
    assert result.rules_fired == (
        "it.notice_text_in_supplier_name",
        "generic.name_contains_url",
    )
    assert result.counters["it.notice_text_in_supplier_name"] == 2


def test_only_the_offending_supplier_is_withheld():
    result = run_stage(facts(
        org(ITALIAN_JUNK),
        org("Alfa S.p.A.", org_id="ORG-2"),
        notice_language="ITA",
    ))
    assert set(result.withheld) == {"ORG-1"}


def test_identifiers_cover_every_organisation_even_when_no_rule_fires():
    result = run_stage(facts(
        buyer("Comune", country="ITA", legal_id="IT00123456789"),
        org("Alfa S.p.A.", country="ITA"),
    ))
    assert result.identifiers == {"BUYER-1": "IT00123456789", "ORG-1": None}
    assert not result.fired


def test_outcomes_are_kept_for_the_report():
    result = run_stage(facts(org(ITALIAN_JUNK), notice_language="ITA"))
    decisions = [o.example()["decision"] for o in result.outcomes]
    assert decisions == ["withheld", "withheld"]


def test_the_stage_runs_without_injected_lookups():
    """Every rule that needs injected data must stay inactive rather
    than raise when the loader could not load it."""
    result = run_stage(facts(org("Alfa S.p.A."), value=value(
        1e12, country="PRT", cpv="32420000")))
    assert "generic.value_peer_outlier" not in result.counters


def test_name_and_value_findings_coexist():
    stats = Lookups(peer_stats=InMemoryPeerStats(
        {("PRT", "3242"): PeerBand(n=412, p10=8e3, median=4.5e4, p90=1.8e5,
                                   p99=9e5)}))
    result = run_stage(
        facts(org(ITALIAN_JUNK), value=value(24474133.0, country="PRT",
                                             cpv="32420000"),
              notice_language="ITA"),
        stats,
    )
    assert result.withheld == {"ORG-1": "it.notice_text_in_supplier_name"}
    assert isinstance(result.value, Quarantine)
