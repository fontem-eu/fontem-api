"""Tests for the TED contract loader."""
from unittest.mock import MagicMock, patch

from src.etl.load_ted_contracts import load_contracts


def _mock_driver_and_session():
    """Create a mock Neo4j driver with session."""
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_load_creates_constraints(mock_matcher_cls, mock_stream):
    """Loader creates contract and authority constraints."""
    mock_stream.return_value = iter([])
    mock_matcher_cls.return_value.stats.summary.return_value = {
        "total": 0, "by_layer": {}, "vies_failures": 0,
    }
    driver, session = _mock_driver_and_session()

    load_contracts(driver, "/fake/path.tar.gz")

    constraint_calls = [
        c for c in session.run.call_args_list
        if c.args and "CONSTRAINT" in c.args[0]
    ]
    assert len(constraint_calls) >= 2
