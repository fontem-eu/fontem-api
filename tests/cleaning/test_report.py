"""The dry-run report: what the stage would do, counted and sampled."""
from src.etl.cleaning.outcomes import Quarantine, Rescale
from src.etl.cleaning.report import CleaningReport
from src.etl.cleaning.stage import CleaningResult, run_stage

from .factories import facts, org

ITALIAN_JUNK = ("Gara aggiudicata come da determina n. 543 del 2013 "
                "pubblicata sul sito www.csc.sanita.fvg.it")


def _record(report, result, notice_id="1-2026", country="ITA", year="2026"):
    report.record(notice_id=notice_id, country=country, year=year, result=result)


def test_a_clean_notice_only_moves_the_total():
    report = CleaningReport()
    _record(report, run_stage(facts(org("Alfa S.p.A."))))
    assert report.as_dict()["totals"] == {
        "notices": 1, "notices_with_rules": 0, "suppliers_withheld": 0,
        "identifiers_normalised": 0, "values_rescaled": 0,
        "values_quarantined": 0,
    }


def test_counts_split_by_rule_country_and_year():
    report = CleaningReport()
    _record(report, run_stage(facts(org(ITALIAN_JUNK), notice_language="ITA")))
    _record(report, run_stage(facts(org(ITALIAN_JUNK), notice_language="ITA")),
            notice_id="2-2025", country="PRT", year="2025")
    out = report.as_dict()
    assert out["totals"]["notices"] == 2
    assert out["totals"]["suppliers_withheld"] == 2
    assert out["by_rule"]["it.notice_text_in_supplier_name"] == 2
    assert set(out["by_country"]) == {"ITA", "PRT"}
    assert out["by_year"]["2025"]["generic.name_contains_url"] == 1


def test_a_missing_country_or_year_is_bucketed_not_dropped():
    report = CleaningReport()
    _record(report, run_stage(facts(org(ITALIAN_JUNK))), country=None, year=None)
    out = report.as_dict()
    assert "?" in out["by_country"] and "?" in out["by_year"]


def test_examples_are_capped_per_rule():
    report = CleaningReport(max_examples=2)
    for i in range(5):
        _record(report, run_stage(facts(org(ITALIAN_JUNK))), notice_id=f"{i}-2026")
    examples = report.as_dict()["examples"]["it.notice_text_in_supplier_name"]
    assert len(examples) == 2
    assert examples[0]["notice_id"] == "0-2026"
    assert examples[0]["decision"] == "withheld"
    assert examples[0]["name_raw"].startswith("Gara aggiudicata")


def test_value_decisions_are_counted_once_per_notice():
    report = CleaningReport()
    _record(report, CleaningResult(rules_fired=("x",),
                                   value=Rescale(0.001, "ratio", "d", None)))
    _record(report, CleaningResult(rules_fired=("x",),
                                   value=Quarantine("implausible_vs_peers", "d")))
    totals = report.as_dict()["totals"]
    assert totals["values_rescaled"] == 1
    assert totals["values_quarantined"] == 1


def test_the_summary_line_names_the_rules():
    report = CleaningReport()
    _record(report, run_stage(facts(org(ITALIAN_JUNK), notice_language="ITA")))
    line = report.summary_line()
    assert "1 notices" in line and "1 suppliers withheld" in line
    assert "it.notice_text_in_supplier_name=1" in line
