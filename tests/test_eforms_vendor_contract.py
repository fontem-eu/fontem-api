"""Guard against eforms-parser wheel skew.

The loader reads ``notice.nuts`` and ``buyer.nuts`` (an Organization).
Unit tests mock the parser, so a vendored wheel that predates those
fields passes every mocked test yet AttributeErrors in production (it
did: the archive path crashed on ``buyer.nuts`` against 0.8.0). This
asserts the ACTUAL vendored dataclasses expose the fields the loader
depends on — cheap insurance against re-introducing the skew.
"""
from eforms.models import Notice, Organization


def test_notice_exposes_nuts():
    assert hasattr(Notice(notice_id="x"), "nuts")


def test_organization_exposes_nuts():
    assert hasattr(Organization(org_id="o", name="n"), "nuts")
