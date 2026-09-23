"""Guard against eforms-parser wheel skew.

The loader reads ``notice.nuts`` and ``buyer.nuts`` (an Organization).
Unit tests mock the parser, so a vendored wheel that predates those
fields passes every mocked test yet AttributeErrors in production (it
did: the archive path crashed on ``buyer.nuts`` against 0.8.0). This
asserts the ACTUAL vendored dataclasses expose the fields the loader
depends on — cheap insurance against re-introducing the skew.
"""
from eforms.models import Award, Lot, Notice, Organization


def test_notice_exposes_nuts():
    assert hasattr(Notice(notice_id="x"), "nuts")


def test_organization_exposes_nuts():
    assert hasattr(Organization(org_id="o", name="n"), "nuts")


def test_award_exposes_the_raw_signals_the_cleaning_stage_reads():
    """eforms-parser 0.12: the verbatim text behind the cleaned money and
    dates, which the cleaning stage keeps on the event."""
    award = Award(lot_id="l", contractor_org_id="o")
    for field in ("award_date_raw", "tender_reference", "value_raw",
                  "framework_max_value", "framework_reestimated_value"):
        assert hasattr(award, field), field


def test_notice_exposes_the_watermark_and_framework_fields():
    notice = Notice(notice_id="x")
    for field in ("tender_result_award_date_raw", "notice_language",
                  "customization_id", "total_value_raw", "framework_max_value",
                  "framework_max_value_currency", "framework_reestimated_value",
                  "framework_duration_months", "framework_max_operators"):
        assert hasattr(notice, field), field
    assert hasattr(Lot(lot_id="l"), "estimated_value_raw")


def test_notice_exposes_the_framework_grouping_key():
    """eforms-parser 0.13: OPT-100, already normalised (the zero padding
    off the publication-number form, the `-NN` version suffix off the
    eForms UUID form). The loader reads it defensively, so a wheel
    without these fields would silently stop grouping frameworks rather
    than fail — this is what notices the skew instead."""
    notice = Notice(notice_id="x")
    for field in ("framework_notice_id", "framework_notice_id_raw",
                  "framework_notice_id_source", "framework_notice_id_conflict"):
        assert hasattr(notice, field), field
