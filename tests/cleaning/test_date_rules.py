"""The sentinel-date rule: counted, never silently dropped."""
import pytest

from src.etl.cleaning.rules.dates import DatePlaceholderRule, is_placeholder_day

from .factories import facts, org

RULE = DatePlaceholderRule()


@pytest.mark.parametrize("raw", [
    "2000-01-01", "1900-01-01", "2000-01-01+01:00", "2000-01-01T00:00:00Z",
])
def test_recognises_the_sentinels(raw):
    assert is_placeholder_day(raw)


@pytest.mark.parametrize("raw", ["2025-09-15", "2000-01-02", None, 20000101, ""])
def test_leaves_real_values_alone(raw):
    assert not is_placeholder_day(raw)


def test_one_outcome_per_sentinel_field():
    notice = facts(
        org("A"),
        award_date_raw="2000-01-01+01:00",
        tender_result_award_date_raw="2000-01-01",
        publication_date_raw="2025-09-04",
        issue_date_raw="1900-01-01",
    )
    outcomes = RULE.decide(notice, None)
    assert [o.subject for o in outcomes] == [
        "award_date_raw", "tender_result_award_date_raw", "issue_date_raw"]
    assert outcomes[0].raw == "2000-01-01+01:00"
    assert outcomes[0].example()["decision"].startswith("placeholder")


def test_a_notice_with_only_real_dates_produces_nothing():
    notice = facts(org("A"), publication_date_raw="2025-09-04")
    assert RULE.applies(notice, None)
    assert not RULE.decide(notice, None)


def test_a_notice_without_dates_skips_the_rule():
    assert not RULE.applies(facts(org("A")), None)
