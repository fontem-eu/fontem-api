"""Tests for the GLEIF XML parser and event-log loader."""
import io
from unittest.mock import MagicMock, patch
from xml.etree.ElementTree import fromstring

from src.etl.load_gleif import (
    _text,
    emit_gleif,
    parse_gleif_xml,
    resolve_latest_url,
)

NS = "http://www.gleif.org/data/schema/leidata/2016"


def _make_xml(records_xml: str) -> io.BytesIO:
    """Wrap record XML fragments in the LEI-CDF envelope."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<lei:LEIData xmlns:lei="{NS}">
<lei:LEIRecords>
{records_xml}
</lei:LEIRecords>
</lei:LEIData>"""
    return io.BytesIO(xml.encode("utf-8"))


ADYEN_XML = f"""
<lei:LEIRecord xmlns:lei="{NS}">
  <lei:LEI>724500973ODKK3IFQ447</lei:LEI>
  <lei:Entity>
    <lei:LegalName>Adyen N.V.</lei:LegalName>
    <lei:LegalAddress>
      <lei:Country>NL</lei:Country>
      <lei:PostalCode>1077 ZX</lei:PostalCode>
    </lei:LegalAddress>
    <lei:LegalForm>
      <lei:OtherLegalForm>N.V.</lei:OtherLegalForm>
    </lei:LegalForm>
    <lei:EntityStatus>ACTIVE</lei:EntityStatus>
  </lei:Entity>
</lei:LEIRecord>
"""

INACTIVE_XML = f"""
<lei:LEIRecord xmlns:lei="{NS}">
  <lei:LEI>213800ZL2PEC4C6UOQ53</lei:LEI>
  <lei:Entity>
    <lei:LegalName>Defunct Ltd</lei:LegalName>
    <lei:LegalAddress><lei:Country>GB</lei:Country></lei:LegalAddress>
    <lei:EntityStatus>INACTIVE</lei:EntityStatus>
  </lei:Entity>
</lei:LEIRecord>
"""

SHORT_LEI_XML = f"""
<lei:LEIRecord xmlns:lei="{NS}">
  <lei:LEI>TOOSHORT</lei:LEI>
  <lei:Entity>
    <lei:LegalName>Bad Corp</lei:LegalName>
    <lei:LegalAddress><lei:Country>US</lei:Country></lei:LegalAddress>
    <lei:EntityStatus>ACTIVE</lei:EntityStatus>
  </lei:Entity>
</lei:LEIRecord>
"""


# ── parse_gleif_xml ──────────────────────────────────────────────────

def test_parse_single_record():
    stream = _make_xml(ADYEN_XML)
    results = list(parse_gleif_xml(stream))
    assert len(results) == 1
    r = results[0]
    assert r["lei"] == "724500973ODKK3IFQ447"
    assert r["name"] == "Adyen N.V."
    # GLEIF XML stores alpha-2 ("NL"); loader normalises to alpha-3 ("NLD")
    # so downstream joins use the internal convention.
    assert r["country"] == "NLD"
    assert r["postal_code"] == "1077 ZX"
    assert r["legal_form"] == "N.V."
    assert r["active"] is True


def test_parse_inactive_entity():
    stream = _make_xml(INACTIVE_XML)
    results = list(parse_gleif_xml(stream))
    assert len(results) == 1
    assert results[0]["active"] is False


def test_parse_skips_short_lei():
    stream = _make_xml(SHORT_LEI_XML)
    results = list(parse_gleif_xml(stream))
    assert len(results) == 0


def test_parse_multiple_records():
    stream = _make_xml(ADYEN_XML + INACTIVE_XML)
    results = list(parse_gleif_xml(stream))
    assert len(results) == 2


def test_parse_empty_records():
    stream = _make_xml("")
    results = list(parse_gleif_xml(stream))
    assert len(results) == 0


def test_parse_missing_legal_form():
    xml = f"""
    <lei:LEIRecord xmlns:lei="{NS}">
      <lei:LEI>529900D69KFL8IAP8Q63</lei:LEI>
      <lei:Entity>
        <lei:LegalName>Some Corp</lei:LegalName>
        <lei:LegalAddress><lei:Country>DK</lei:Country></lei:LegalAddress>
        <lei:EntityStatus>ACTIVE</lei:EntityStatus>
      </lei:Entity>
    </lei:LEIRecord>
    """
    results = list(parse_gleif_xml(_make_xml(xml)))
    assert len(results) == 1
    assert results[0]["legal_form"] is None


def test_parse_entity_legal_form_code():
    xml = f"""
    <lei:LEIRecord xmlns:lei="{NS}">
      <lei:LEI>529900D69KFL8IAP8Q63</lei:LEI>
      <lei:Entity>
        <lei:LegalName>Code Corp</lei:LegalName>
        <lei:LegalAddress><lei:Country>DE</lei:Country></lei:LegalAddress>
        <lei:LegalForm>
          <lei:EntityLegalFormCode>8888</lei:EntityLegalFormCode>
        </lei:LegalForm>
        <lei:EntityStatus>ACTIVE</lei:EntityStatus>
      </lei:Entity>
    </lei:LEIRecord>
    """
    results = list(parse_gleif_xml(_make_xml(xml)))
    assert results[0]["legal_form"] == "8888"


# ── _text helper ─────────────────────────────────────────────────────

def test_text_returns_content():
    """_text extracts child element text."""
    el = fromstring("<root><child>hello</child></root>")
    assert _text(el, "child") == "hello"


def test_text_returns_none_for_missing():
    """_text returns None when child is absent."""
    el = fromstring("<root></root>")
    assert _text(el, "child") is None


# ── emit_gleif ──────────────────────────────────────────────────────

def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def test_emit_gleif_one_event_per_record():
    """One UpsertCompany event per LEI; LEI passed through verbatim."""
    log, emit = _mock_log()
    records = iter([
        {"lei": "724500973ODKK3IFQ447", "name": "Adyen N.V.",
         "country": "NL", "postal_code": "1077 ZX",
         "legal_form": "N.V.", "active": True},
        {"lei": "529900D69KFL8IAP8Q63", "name": "Code Corp",
         "country": "DE", "postal_code": None,
         "legal_form": "8888", "active": True},
    ])
    summary = emit_gleif(log, records)
    assert summary["total"] == 2
    assert emit.upsert.call_count == 2
    assert all(c.args[0] == "UpsertCompany" for c in emit.upsert.call_args_list)
    payloads = [c.kwargs["payload"] for c in emit.upsert.call_args_list]
    assert payloads[0]["lei"] == "724500973ODKK3IFQ447"
    assert payloads[0]["country"] == "NL"
    assert payloads[0]["legal_form"] == "N.V."
    # gmr_id is deterministic from LEI; we don't assert the exact value
    # here (it's gmr_id.from_lei's contract) but it must be present.
    assert "gmr_id" in payloads[0]


def test_emit_gleif_no_bracket_for_incremental_load():
    """GLEIF dump is multi-million records with overlap across other
    sources; the loader must NOT bracket-replace the Company graph."""
    log, emit = _mock_log()
    records = iter([
        {"lei": "724500973ODKK3IFQ447", "name": "Adyen",
         "country": "NL", "postal_code": None,
         "legal_form": None, "active": True},
    ])
    emit_gleif(log, records)
    emit.control.assert_not_called()


def test_emit_gleif_active_status_preserved():
    log, emit = _mock_log()
    records = iter([
        {"lei": "213800ZL2PEC4C6UOQ53", "name": "Defunct Ltd",
         "country": "GB", "postal_code": None,
         "legal_form": None, "active": False},
    ])
    emit_gleif(log, records)
    payload = emit.upsert.call_args.kwargs["payload"]
    assert payload["active"] is False


# ── resolve_latest_url ───────────────────────────────────────────────

def test_resolve_latest_url():
    """resolve_latest_url builds the correct download URL from the API."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": [{"id": 40690}]
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("src.etl.load_gleif.httpx.get", return_value=mock_resp):
        url = resolve_latest_url()

    assert "40690" in url
    assert url.endswith("/zip")


# ── GLEIF identity block (real LEI-CDF v3.1 structure + real values) ──

# Carlsberg A/S — real GLEIF record (LEI 5299001O0WJQYB5GYZ19): GENERAL
# entity, Danish CVR register RA000170 / 61056416.
CARLSBERG_XML = f"""
<lei:LEIRecord xmlns:lei="{NS}">
  <lei:LEI>5299001O0WJQYB5GYZ19</lei:LEI>
  <lei:Entity>
    <lei:LegalName>CARLSBERG A/S</lei:LegalName>
    <lei:OtherEntityNames>
      <lei:OtherEntityName type="PREVIOUS_LEGAL_NAME">Carlsberg Group</lei:OtherEntityName>
    </lei:OtherEntityNames>
    <lei:LegalAddress>
      <lei:FirstAddressLine>J.C. Jacobsens Gade 1</lei:FirstAddressLine>
      <lei:City>København V</lei:City>
      <lei:Region>DK-84</lei:Region>
      <lei:Country>DK</lei:Country>
      <lei:PostalCode>1799</lei:PostalCode>
    </lei:LegalAddress>
    <lei:HeadquartersAddress>
      <lei:FirstAddressLine>J.C. Jacobsens Gade 1</lei:FirstAddressLine>
      <lei:City>København V</lei:City>
      <lei:Country>DK</lei:Country>
      <lei:PostalCode>1799</lei:PostalCode>
    </lei:HeadquartersAddress>
    <lei:RegistrationAuthority>
      <lei:RegistrationAuthorityID>RA000170</lei:RegistrationAuthorityID>
      <lei:RegistrationAuthorityEntityID>61056416</lei:RegistrationAuthorityEntityID>
    </lei:RegistrationAuthority>
    <lei:LegalJurisdiction>DK</lei:LegalJurisdiction>
    <lei:EntityCategory>GENERAL</lei:EntityCategory>
    <lei:LegalForm><lei:EntityLegalFormCode>ZRPO</lei:EntityLegalFormCode></lei:LegalForm>
    <lei:EntityStatus>ACTIVE</lei:EntityStatus>
    <lei:EntityCreationDate>1999-10-16T00:00:00Z</lei:EntityCreationDate>
  </lei:Entity>
  <lei:Registration>
    <lei:RegistrationStatus>ISSUED</lei:RegistrationStatus>
  </lei:Registration>
</lei:LEIRecord>
"""

# AGILIS — real GLEIF record (LEI 969500BWXPDCRLHC3Z76): category FUND.
AGILIS_FUND_XML = f"""
<lei:LEIRecord xmlns:lei="{NS}">
  <lei:LEI>969500BWXPDCRLHC3Z76</lei:LEI>
  <lei:Entity>
    <lei:LegalName>AGILIS</lei:LegalName>
    <lei:LegalAddress><lei:Country>FR</lei:Country><lei:PostalCode>75016</lei:PostalCode></lei:LegalAddress>
    <lei:EntityCategory>FUND</lei:EntityCategory>
    <lei:LegalForm><lei:EntityLegalFormCode>MQU9</lei:EntityLegalFormCode></lei:LegalForm>
    <lei:EntityStatus>ACTIVE</lei:EntityStatus>
  </lei:Entity>
</lei:LEIRecord>
"""


def test_parse_extracts_full_identity_block():
    rec = next(parse_gleif_xml(_make_xml(CARLSBERG_XML)))
    assert rec["entity_kind"] == "GENERAL"
    assert rec["registered_as"] == "61056416"
    assert rec["registered_at"] == "RA000170"
    assert rec["jurisdiction"] == "DK"
    assert rec["registration_status"] == "ISSUED"
    assert rec["entity_creation_date"].startswith("1999-10-16")
    assert rec["address"] == "J.C. Jacobsens Gade 1"
    assert rec["city"] == "København V"
    assert rec["region"] == "DK-84"
    assert rec["hq_city"] == "København V"
    assert rec["hq_country"] == "DNK"          # alpha-2 -> alpha-3
    assert rec["aliases"] == ["Carlsberg Group"]


def test_parse_fund_category_verbatim():
    rec = next(parse_gleif_xml(_make_xml(AGILIS_FUND_XML)))
    assert rec["entity_kind"] == "FUND"
    # no OtherEntityNames -> aliases absent, not empty list
    assert rec["aliases"] is None


def test_parse_missing_identity_fields_are_none_not_crash():
    # ADYEN_XML has none of the new blocks — every new field degrades to
    # None (a wrong tag name would surface here as a coverage-zero, and
    # in prod as the dq entity_kind-coverage assertion, before prod).
    rec = next(parse_gleif_xml(_make_xml(ADYEN_XML)))
    for k in ("entity_kind", "registered_as", "jurisdiction",
              "hq_city", "aliases", "registration_status"):
        assert rec[k] is None, k


def test_emit_threads_identity_block():
    rec = next(parse_gleif_xml(_make_xml(CARLSBERG_XML)))
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    emit_gleif(log, [rec])
    payload = emit.upsert.call_args.kwargs["payload"]
    assert payload["entity_kind"] == "GENERAL"
    assert payload["registered_as"] == "61056416"
    assert payload["aliases"] == ["Carlsberg Group"]
