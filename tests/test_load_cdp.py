"""Regression tests for the CDP climate-disclosure loader.

The previous fuzzy path (`MATCH_COMPANY_FUZZY`) used score>2.0 with
a NULL-bypassing country guard and SET cdp_score / scope1_emissions
on the top-scoring candidate. There were 0 cdp_score writes in
production yet — the bug is dormant — but the shape is identical to
the lobbying and sanctions disasters: misattributed climate data
would say "Company X has Y kg CO2" when it's a different Y.

The fuzzy path was removed entirely. Only exact name+country match
is supported; CDP rows that don't cleanly match are silently
skipped. The full /resolve service migration is tracked separately.
"""
from __future__ import annotations

from src.etl import load_cdp
from src.etl.load_cdp import (
    UPDATE_COMPANY_EXACT,
    load_into_neo4j,
)


def test_no_fuzzy_match_cypher_exists():
    """The fuzzy path was the source of the misattribution risk. It
    must not exist as a callable cypher anywhere in this module —
    re-introducing it must require explicit code review."""
    assert not hasattr(load_cdp, "MATCH_COMPANY_FUZZY"), (
        "MATCH_COMPANY_FUZZY must remain removed. If you need fuzzy "
        "matching here, route the request through gmr-consolidator's "
        "/resolve endpoint, which has the proper guards + audit trail."
    )
    assert not hasattr(load_cdp, "CREATE_FT_INDEX"), (
        "CDP loader no longer needs the fulltext index — it only does "
        "exact name+country matches."
    )


def test_exact_match_requires_non_empty_country():
    """Same-name companies in different jurisdictions must not get
    cross-pollinated CDP properties. Empty/NULL country on the CDP
    row must short-circuit the match."""
    assert "coalesce(row.country, '') <> ''" in UPDATE_COMPANY_EXACT


def test_exact_match_uses_country_equality():
    assert "c.country = row.country" in UPDATE_COMPANY_EXACT


def test_load_into_neo4j_returns_no_fuzzy_count():
    """The summary dict must not advertise a fuzzy_updated counter —
    callers that depend on the old shape need to migrate."""
    from unittest.mock import MagicMock  # pylint: disable=import-outside-toplevel

    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    counters = MagicMock()
    counters.properties_set = 0
    mock_session.run.return_value.consume.return_value = counters

    summary = load_into_neo4j(mock_driver, [])
    assert "fuzzy_updated" not in summary
    assert "exact_updated" in summary


def test_loader_does_not_create_fulltext_index():
    """The fulltext index was only needed for the removed fuzzy path.
    Don't pollute the schema. Verify session.run was never called with
    a CREATE FULLTEXT INDEX statement."""
    from unittest.mock import MagicMock  # pylint: disable=import-outside-toplevel

    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    counters = MagicMock()
    counters.properties_set = 0
    mock_session.run.return_value.consume.return_value = counters

    load_into_neo4j(mock_driver, [{"company_name": "Foo", "country": "DEU",
                                    "cdp_score": "A", "scope1_emissions": 1.0,
                                    "scope2_emissions": 1.0, "reporting_year": 2025}])

    for call in mock_session.run.call_args_list:
        query = call.args[0] if call.args else ""
        assert "CREATE FULLTEXT INDEX" not in query, (
            f"CDP loader must not create the fulltext index anymore: {query[:80]}"
        )
