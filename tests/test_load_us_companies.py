"""Tests for the US companies loader."""
from unittest.mock import MagicMock

from src.etl.load_us_companies import load_us_companies


def _mock_driver():
    """Create a mock Neo4j driver with a usable session context manager."""
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session


def test_creates_index_and_merges():
    """Loader creates a CIK index then MERGEs each company."""
    driver, session = _mock_driver()
    data = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    }
    total = load_us_companies(driver, data)
    assert total == 1
    calls = session.run.call_args_list
    assert "INDEX" in calls[0].args[0]
    assert "MERGE" in calls[1].args[0]


def test_batches_multiple_companies():
    """Multiple companies are batched correctly."""
    driver, session = _mock_driver()
    data = {
        str(i): {"cik_str": i + 1, "ticker": f"T{i}", "title": f"Co {i}"}
        for i in range(5)
    }
    total = load_us_companies(driver, data)
    assert total == 5


def test_skips_entries_without_ticker():
    """Entries missing a ticker field are skipped."""
    driver, session = _mock_driver()
    data = {
        "0": {"cik_str": 123, "title": "No Ticker Corp"},
    }
    total = load_us_companies(driver, data)
    assert total == 0


def test_zero_pads_cik():
    """CIK is zero-padded to 10 digits in the batch."""
    driver, session = _mock_driver()
    data = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
    }
    load_us_companies(driver, data)
    batch = session.run.call_args_list[1].kwargs["batch"]
    assert batch[0]["cik"] == "0000320193"


def test_ticker_uppercased():
    """Ticker is uppercased before loading."""
    driver, session = _mock_driver()
    data = {
        "0": {"cik_str": 1, "ticker": "aapl", "title": "Apple"},
    }
    load_us_companies(driver, data)
    batch = session.run.call_args_list[1].kwargs["batch"]
    assert batch[0]["ticker"] == "AAPL"
