"""Tests for the person data source and ETL."""
from unittest.mock import MagicMock

from src.analysis.person_data_source import PersonDataSource
from src.etl.load_fr_directors import _person_id


class MockPersonSource(PersonDataSource):
    """Test implementation."""

    def get_company_directors(self, gmr_id, include_former=False):
        return [
            {"person_id": "pid-1", "name": "DUPONT", "first_name": "JEAN",
             "role": "Président", "current": True},
        ]

    def get_person_roles(self, person_id):
        return [
            {"gmr_id": "gid-1", "company_name": "Test Corp",
             "role": "Président", "current": True},
        ]

    def search_persons(self, name, limit=10):
        return [
            {"person_id": "pid-1", "name": "DUPONT",
             "first_name": "JEAN", "companies": ["Test Corp"]},
        ]


def test_person_id_deterministic():
    """Same inputs produce the same person_id."""
    a = _person_id("DUPONT", "JEAN", "1970")
    b = _person_id("DUPONT", "JEAN", "1970")
    assert a == b


def test_person_id_differs_by_name():
    """Different names produce different IDs."""
    a = _person_id("DUPONT", "JEAN", "1970")
    b = _person_id("MARTIN", "JEAN", "1970")
    assert a != b


def test_person_id_case_insensitive():
    """Name matching is case-insensitive."""
    a = _person_id("dupont", "jean", "1970")
    b = _person_id("DUPONT", "JEAN", "1970")
    assert a == b


def test_mock_source_get_directors():
    """MockPersonSource returns directors."""
    src = MockPersonSource()
    dirs = src.get_company_directors("gid-1")
    assert len(dirs) == 1
    assert dirs[0]["name"] == "DUPONT"


def test_mock_source_search():
    """MockPersonSource search returns results."""
    src = MockPersonSource()
    results = src.search_persons("dupont")
    assert len(results) == 1
    assert results[0]["companies"] == ["Test Corp"]
