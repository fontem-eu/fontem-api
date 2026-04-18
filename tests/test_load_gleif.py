"""Tests for the GLEIF XML parser and Neo4j loader."""
import io
from unittest.mock import MagicMock, patch
from xml.etree.ElementTree import fromstring

from src.etl.load_gleif import (
    _text,
    load_into_neo4j,
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
    assert r["country"] == "NL"
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
    assert results[0]["legal_form"] == ""


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


# ── load_into_neo4j ─────────────────────────────────────────────────

def test_load_creates_constraint_and_merges():
    """Loader creates a uniqueness constraint then MERGEs records."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(
        return_value=mock_session
    )
    mock_driver.session.return_value.__exit__ = MagicMock(
        return_value=False
    )

    records = iter([
        {"lei": "724500973ODKK3IFQ447", "name": "Adyen",
         "country": "NL", "postal_code": "1077 ZX", "legal_form": "N.V.", "active": True},
    ])

    summary = load_into_neo4j(mock_driver, records, batch_size=100)

    assert summary["total"] == 1
    # First call: CREATE CONSTRAINT, second: UNWIND MERGE
    calls = mock_session.run.call_args_list
    assert "CONSTRAINT" in calls[0].args[0]
    assert "MERGE" in calls[1].args[0]


def test_load_batches_correctly():
    """Five records with batch_size=2 yields three MERGE calls."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(
        return_value=mock_session
    )
    mock_driver.session.return_value.__exit__ = MagicMock(
        return_value=False
    )

    # 5 records with batch_size=2 → 3 batches (2+2+1)
    records = iter([
        {"lei": f"{'A' * 18}{i:02d}", "name": f"Co{i}",
         "country": "XX", "postal_code": "", "legal_form": "", "active": True}
        for i in range(5)
    ])

    summary = load_into_neo4j(mock_driver, records, batch_size=2)
    assert summary["total"] == 5
    # 1 constraint call + 3 batch calls
    merge_calls = [
        c for c in mock_session.run.call_args_list
        if "MERGE" in c.args[0]
    ]
    assert len(merge_calls) == 3


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
