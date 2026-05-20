"""Tests for the US companies loader.

Post-event-log: the loader emits UpsertCompany + UpsertListing events
into the event log via fontem_events.EventLog. Sinks project them; the
loader itself never touches Neo4j or Virtuoso directly.
"""
from unittest.mock import MagicMock

from src.etl.load_us_companies import load_us_companies


def _mock_log():
    """Create a mock EventLog that records every emit() call."""
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def test_emits_company_and_listing_per_row():
    log, emit = _mock_log()
    data = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    }
    total = load_us_companies(log, data)
    assert total == 1
    # Two emits per row: UpsertCompany + UpsertListing.
    assert emit.upsert.call_count == 2
    types = [c.args[0] for c in emit.upsert.call_args_list]
    assert types == ["UpsertCompany", "UpsertListing"]


def test_company_payload_carries_cik_and_country():
    log, emit = _mock_log()
    data = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    }
    load_us_companies(log, data)
    company_call = emit.upsert.call_args_list[0]
    payload = company_call.kwargs["payload"]
    assert payload["country"] == "US"
    assert payload["cik"] == "0000320193"
    assert payload["active"] is True


def test_listing_payload_links_to_company():
    log, emit = _mock_log()
    data = {
        "0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple"},
    }
    load_us_companies(log, data)
    company_payload = emit.upsert.call_args_list[0].kwargs["payload"]
    listing_payload = emit.upsert.call_args_list[1].kwargs["payload"]
    # Ticker uppercased; Listing carries Company's gmr_id.
    assert listing_payload["ticker"] == "AAPL"
    assert listing_payload["company_gmr_id"] == company_payload["gmr_id"]
    assert listing_payload["exchange"] == "US"
    assert listing_payload["currency"] == "USD"


def test_skips_entries_without_ticker():
    log, emit = _mock_log()
    data = {"0": {"cik_str": 123, "title": "No Ticker Corp"}}
    total = load_us_companies(log, data)
    assert total == 0
    emit.upsert.assert_not_called()


def test_skips_entries_without_cik():
    log, emit = _mock_log()
    data = {"0": {"ticker": "ZZZ", "title": "No CIK Co"}}
    total = load_us_companies(log, data)
    assert total == 0
    emit.upsert.assert_not_called()


def test_emits_within_single_batch_transaction():
    """All upserts happen inside one log.batch() context — sinks see
    them as a single transactional group, even though no Begin/End
    bracket is used (this is incremental upsert, not bulk replace)."""
    log, _emit = _mock_log()
    data = {
        str(i): {"cik_str": i + 1, "ticker": f"T{i}", "title": f"Co {i}"}
        for i in range(3)
    }
    load_us_companies(log, data)
    assert log.batch.call_count == 1
