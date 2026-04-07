"""Tests for the CPV reference data loader."""
from unittest.mock import MagicMock

from src.etl.load_cpv import CPV_DIVISIONS, CPV_DETAILED, load_cpv_divisions


def test_load_creates_constraint_and_merges():
    """Loader creates CPV constraint then MERGEs divisions + detailed codes."""
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)

    count = load_cpv_divisions(driver)
    assert count == len(CPV_DIVISIONS) + len(CPV_DETAILED)
    calls = session.run.call_args_list
    assert "CONSTRAINT" in calls[0].args[0]
    assert "MERGE" in calls[1].args[0]


def test_cpv_divisions_have_standard_codes():
    """CPV division codes are 2-digit strings."""
    for code in CPV_DIVISIONS:
        assert len(code) == 2
        assert code.isdigit()
