"""Tests for the gmr_id module — deterministic UUID5 generation."""
# pylint: disable=missing-function-docstring
import uuid

from src.etl import gmr_id


def test_from_lei_returns_valid_uuid():
    result = gmr_id.from_lei("549300EEJH4FEPDBBR25")
    uuid.UUID(result)  # raises if malformed


def test_from_lei_is_deterministic():
    a = gmr_id.from_lei("549300EEJH4FEPDBBR25")
    b = gmr_id.from_lei("549300EEJH4FEPDBBR25")
    assert a == b


def test_from_lei_different_leis_differ():
    a = gmr_id.from_lei("549300EEJH4FEPDBBR25")
    b = gmr_id.from_lei("724500973ODKK3IFQ447")
    assert a != b


def test_from_cik_zero_pads():
    a = gmr_id.from_cik("320193")
    b = gmr_id.from_cik(320193)
    assert a == b
    # Both should use edgar:0000320193
    expected = str(uuid.uuid5(gmr_id.GMR_NAMESPACE, "edgar:0000320193"))
    assert a == expected


def test_from_cik_already_padded():
    assert gmr_id.from_cik("0000320193") == gmr_id.from_cik(320193)


def test_from_national():
    result = gmr_id.from_national("GB", "05765016")
    expected = str(uuid.uuid5(gmr_id.GMR_NAMESPACE, "GB:05765016"))
    assert result == expected


def test_from_name_normalizes():
    a = gmr_id.from_name("ES", "  Telefonica S.A.  ")
    b = gmr_id.from_name("ES", "TELEFONICA S.A.")
    assert a == b


def test_from_name_different_countries_differ():
    a = gmr_id.from_name("ES", "ACME Corp")
    b = gmr_id.from_name("GB", "ACME Corp")
    assert a != b
