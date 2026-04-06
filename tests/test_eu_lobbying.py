"""Unit tests for the EU Lobbying Register parser."""
import xml.etree.ElementTree as ET

from src.etl.load_eu_lobbying import _parse_entity, _text


SAMPLE_XML = """<interestRepresentative>
  <identificationCode>12345678-90</identificationCode>
  <registrationDate>2020-03-15T10:00:00.000+00:00</registrationDate>
  <lastUpdateDate>2025-11-01T14:30:00.000+00:00</lastUpdateDate>
  <name><originalName>Test Lobbyist Corp</originalName></name>
  <acronym>TLC</acronym>
  <entityForm>Company</entityForm>
  <webSiteURL>https://example.com</webSiteURL>
  <registrationCategory>In-house lobbyists</registrationCategory>
  <headOffice>
    <address>123 Rue de la Loi</address>
    <postCode>1000</postCode>
    <city>Brussels</city>
    <country>BELGIUM</country>
    <phone><indicPhone>32</indicPhone><phoneNumber>123456</phoneNumber></phone>
  </headOffice>
  <goals>Promoting transparency in EU legislation</goals>
  <EPAccreditedNumber>3</EPAccreditedNumber>
  <members><membersFTE>12.5</membersFTE></members>
  <interests>
    <interest><name>Digital economy and society</name></interest>
    <interest><name>Research and innovation</name></interest>
  </interests>
  <financialData>
    <closedYear>
      <startDate>2024-01-01</startDate>
      <endDate>2024-12-31</endDate>
      <costs><range><min>100000</min><max>200000</max></range></costs>
    </closedYear>
  </financialData>
</interestRepresentative>"""


def test_parse_entity_basic_fields():
    elem = ET.fromstring(SAMPLE_XML)
    result = _parse_entity(elem)
    assert result["tr_id"] == "12345678-90"
    assert result["name"] == "Test Lobbyist Corp"
    assert result["acronym"] == "TLC"
    assert result["country"] == "BELGIUM"
    assert result["city"] == "Brussels"
    assert result["category"] == "In-house lobbyists"
    assert result["website"] == "https://example.com"


def test_parse_entity_dates():
    elem = ET.fromstring(SAMPLE_XML)
    result = _parse_entity(elem)
    assert result["registration_date"] == "2020-03-15"
    assert result["last_updated"] == "2025-11-01"


def test_parse_entity_ep_passes():
    elem = ET.fromstring(SAMPLE_XML)
    result = _parse_entity(elem)
    assert result["ep_passes"] == 3


def test_parse_entity_members_fte():
    elem = ET.fromstring(SAMPLE_XML)
    result = _parse_entity(elem)
    assert result["members_fte"] == 12.5


def test_parse_entity_financial_data():
    elem = ET.fromstring(SAMPLE_XML)
    result = _parse_entity(elem)
    assert result["cost_min"] == 100000
    assert result["cost_max"] == 200000


def test_parse_entity_interests():
    elem = ET.fromstring(SAMPLE_XML)
    result = _parse_entity(elem)
    assert len(result["interests"]) == 2
    assert "Digital economy and society" in result["interests"]
    assert "Research and innovation" in result["interests"]


def test_parse_entity_goals_truncated():
    elem = ET.fromstring(SAMPLE_XML)
    result = _parse_entity(elem)
    assert len(result["goals"]) <= 500


def test_text_helper_missing_path():
    elem = ET.fromstring("<root><child>value</child></root>")
    assert _text(elem, "nonexistent") == ""
    assert _text(elem, "child") == "value"


def test_text_helper_none_element():
    assert _text(None, "anything") == ""
