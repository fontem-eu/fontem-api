"""Tests for the gmr_id module."""
import uuid

from src.etl.gmr_id import (
    GMR_NAMESPACE,
    from_cik,
    from_lei,
    from_name,
    from_national_id,
)


def test_from_lei_deterministic():
    """Same LEI always produces the same gmr_id."""
    a = from_lei("549300EEJH4FEPDBBR25")
    b = from_lei("549300EEJH4FEPDBBR25")
    assert a == b


def test_from_lei_matches_uuid5():
    """Output matches a direct uuid5 call."""
    lei = "549300EEJH4FEPDBBR25"
    expected = str(uuid.uuid5(GMR_NAMESPACE, f"lei:{lei}"))
    assert from_lei(lei) == expected


def test_from_lei_different_leis_differ():
    """Different LEIs produce different gmr_ids."""
    assert from_lei("AAA") != from_lei("BBB")


def test_from_cik_zero_pads():
    """CIK is zero-padded to 10 digits before hashing."""
    expected = str(uuid.uuid5(GMR_NAMESPACE, "edgar:0000320193"))
    assert from_cik("320193") == expected
    assert from_cik("0000320193") == expected


def test_from_national_id_uppercases_country():
    """Country code is case-insensitive."""
    a = from_national_id("gb", "05765016")
    b = from_national_id("GB", "05765016")
    assert a == b


def test_from_name_normalises():
    """Name is stripped and uppercased before hashing."""
    a = from_name("de", "  Siemens AG  ")
    b = from_name("DE", "SIEMENS AG")
    assert a == b


def test_all_methods_return_valid_uuid():
    """Every factory method returns a valid UUID v5 string."""
    for val in [
        from_lei("549300EEJH4FEPDBBR25"),
        from_cik("320193"),
        from_national_id("GB", "05765016"),
        from_name("DE", "Siemens AG"),
    ]:
        parsed = uuid.UUID(val)
        assert parsed.version == 5
