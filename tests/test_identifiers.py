"""Canon identifier tests in edgar-gmr-etl (duplicated from gmr-consolidator).

The TED loader relies on `canon_vat` to drop non-canonical values that TED
publishers put in `cbc:CompanyID`. The important negative cases are the
4-part hyphenated French tenderer refs like `1518336-1-9-1` that used to
end up in `Company.vat`."""

from src.etl import identifiers as I


def test_french_ted_tenderer_ref_rejected():
    assert I.canon_vat("1518336-1-9-1") is None
    assert I.canon_vat("1515711-1-104-1") is None
    assert I.canon_vat("1743202-1-0-1") is None


def test_bare_siret_rejected_as_vat():
    """A 14-digit SIRET is not a VAT — France's VAT starts with FR."""
    assert I.canon_vat("83415751300815") is None


def test_polish_nip_hyphenated_rejected_as_vat():
    """`118-00-62-976` is a valid Polish NIP but not a Polish VAT (which starts with PL)."""
    assert I.canon_vat("118-00-62-976") is None


def test_valid_vats_accepted_and_canonicalised():
    assert I.canon_vat("DE273691032") == "DE273691032"
    assert I.canon_vat("DE 273 691 032") == "DE273691032"
    assert I.canon_vat("FR12345678901") == "FR12345678901"
    assert I.canon_vat("PL1234567890") == "PL1234567890"


def test_empty_and_none():
    assert I.canon_vat("") is None
    assert I.canon_vat(None) is None
