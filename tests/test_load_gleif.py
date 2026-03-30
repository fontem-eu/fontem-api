"""Tests for the GLEIF ETL — XML parsing and Neo4j loading."""
# pylint: disable=missing-function-docstring
import io
from unittest.mock import MagicMock, patch

import pytest

from src.etl.load_gleif import (
    parse_gleif_xml,
    load_into_neo4j,
    get_latest_download_url,
)

# ── Fixtures ─────────────────────────────────────────────────────────────

SAMPLE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<lei:LEIData
  xmlns:lei="http://www.gleif.org/data/schema/leidata/2016">
  <lei:LEIHeader/>
  <lei:LEIRecords>
    <lei:LEIRecord>
      <lei:LEI>549300EEJH4FEPDBBR25</lei:LEI>
      <lei:Entity>
        <lei:LegalName>Telefonica S.A.</lei:LegalName>
        <lei:LegalAddress>
          <lei:Country>ES</lei:Country>
        </lei:LegalAddress>
        <lei:EntityStatus>ACTIVE</lei:EntityStatus>
        <lei:LegalForm>
          <lei:OtherLegalForm>S.A.</lei:OtherLegalForm>
        </lei:LegalForm>
      </lei:Entity>
    </lei:LEIRecord>
    <lei:LEIRecord>
      <lei:LEI>724500973ODKK3IFQ447</lei:LEI>
      <lei:Entity>
        <lei:LegalName>Adyen N.V.</lei:LegalName>
        <lei:LegalAddress>
          <lei:Country>NL</lei:Country>
        </lei:LegalAddress>
        <lei:EntityStatus>INACTIVE</lei:EntityStatus>
        <lei:LegalForm>
          <lei:EntityLegalFormCode>BV</lei:EntityLegalFormCode>
        </lei:LegalForm>
      </lei:Entity>
    </lei:LEIRecord>
  </lei:LEIRecords>
</lei:LEIData>
"""

SHORT_LEI_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<lei:LEIData
  xmlns:lei="http://www.gleif.org/data/schema/leidata/2016">
  <lei:LEIRecords>
    <lei:LEIRecord>
      <lei:LEI>TOOSHORT</lei:LEI>
      <lei:Entity>
        <lei:LegalName>Bad Corp</lei:LegalName>
        <lei:LegalAddress><lei:Country>XX</lei:Country></lei:LegalAddress>
        <lei:EntityStatus>ACTIVE</lei:EntityStatus>
      </lei:Entity>
    </lei:LEIRecord>
  </lei:LEIRecords>
</lei:LEIData>
"""

EMPTY_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<lei:LEIData
  xmlns:lei="http://www.gleif.org/data/schema/leidata/2016">
  <lei:LEIRecords/>
</lei:LEIData>
"""

NO_ENTITY_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<lei:LEIData
  xmlns:lei="http://www.gleif.org/data/schema/leidata/2016">
  <lei:LEIRecords>
    <lei:LEIRecord>
      <lei:LEI>549300EEJH4FEPDBBR25</lei:LEI>
    </lei:LEIRecord>
  </lei:LEIRecords>
</lei:LEIData>
"""


def _stream(xml_str: str):
    return io.BytesIO(xml_str.encode("utf-8"))


# ── parse_gleif_xml tests ────────────────────────────────────────────────


def test_parse_extracts_two_records():
    records = list(parse_gleif_xml(_stream(SAMPLE_XML)))
    assert len(records) == 2


def test_parse_first_record_fields():
    rec = list(parse_gleif_xml(_stream(SAMPLE_XML)))[0]
    assert rec["lei"] == "549300EEJH4FEPDBBR25"
    assert rec["name"] == "Telefonica S.A."
    assert rec["country"] == "ES"
    assert rec["active"] is True
    assert rec["legal_form"] == "S.A."


def test_parse_inactive_status():
    rec = list(parse_gleif_xml(_stream(SAMPLE_XML)))[1]
    assert rec["active"] is False


def test_parse_legal_form_code_preferred():
    rec = list(parse_gleif_xml(_stream(SAMPLE_XML)))[1]
    assert rec["legal_form"] == "BV"


def test_parse_empty_xml_yields_nothing():
    assert not list(parse_gleif_xml(_stream(EMPTY_XML)))


def test_parse_skips_record_without_entity():
    assert not list(parse_gleif_xml(_stream(NO_ENTITY_XML)))


# ── load_into_neo4j tests ────────────────────────────────────────────────


def _make_driver():
    """Build a mock Neo4j driver whose session context manager records calls."""
    session = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    driver = MagicMock()
    driver.session.return_value = ctx
    # Attach for easy access in tests that need it
    driver._mock_session = session  # pylint: disable=protected-access
    return driver


def test_load_creates_constraint():
    driver = _make_driver()
    load_into_neo4j(driver, iter([]), constraint=True)
    session = driver._mock_session  # pylint: disable=protected-access
    constraint_calls = [
        c for c in session.run.call_args_list
        if "CONSTRAINT" in str(c)
    ]
    assert len(constraint_calls) == 1


def test_load_skips_short_lei():
    driver = _make_driver()
    records = parse_gleif_xml(_stream(SHORT_LEI_XML))
    summary = load_into_neo4j(driver, records, constraint=False)
    assert summary["skipped"] == 1
    assert summary["loaded"] == 0


def test_load_flushes_batch():
    driver = _make_driver()
    records = parse_gleif_xml(_stream(SAMPLE_XML))
    summary = load_into_neo4j(
        driver, records, batch_size=100, constraint=False
    )
    assert summary["loaded"] == 2
    session = driver._mock_session  # pylint: disable=protected-access
    merge_calls = [
        c for c in session.run.call_args_list
        if "MERGE" in str(c)
    ]
    assert len(merge_calls) == 1


def test_load_batches_when_full():
    driver = _make_driver()
    records = parse_gleif_xml(_stream(SAMPLE_XML))
    summary = load_into_neo4j(
        driver, records, batch_size=1, constraint=False
    )
    assert summary["loaded"] == 2
    session = driver._mock_session  # pylint: disable=protected-access
    merge_calls = [
        c for c in session.run.call_args_list
        if "MERGE" in str(c)
    ]
    # batch_size=1 means 2 flushes for 2 records
    assert len(merge_calls) == 2


def test_load_adds_gmr_id_to_each_record():
    driver = _make_driver()
    records = parse_gleif_xml(_stream(SAMPLE_XML))
    load_into_neo4j(driver, records, batch_size=100, constraint=False)
    session = driver._mock_session  # pylint: disable=protected-access
    merge_call = [
        c for c in session.run.call_args_list
        if "MERGE" in str(c)
    ][0]
    batch = merge_call.kwargs.get("batch") or merge_call[1]["batch"]
    for rec in batch:
        assert "gmr_id" in rec
        assert len(rec["gmr_id"]) == 36  # UUID format


# ── get_latest_download_url tests ────────────────────────────────────────


@patch("src.etl.load_gleif.httpx.get")
def test_get_latest_url_parses_api(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": [{"id": 40690}]}
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    url = get_latest_download_url()
    assert "40690" in url
    assert url.endswith("/zip")


@patch("src.etl.load_gleif.httpx.get")
def test_get_latest_url_raises_on_empty(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": []}
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    with pytest.raises(RuntimeError):
        get_latest_download_url()
