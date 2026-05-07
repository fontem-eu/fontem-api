"""Tests for load_openfigi event-log migration."""
from unittest.mock import MagicMock, patch

import httpx

from src.etl import load_openfigi


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def test_emit_listing_events_per_enriched_row():
    log, emit = _mock_log()
    enriched = [
        {"isin": "US0378331005",
         "company_gmr_id": "00040372-dad6-5d34-882c-8b8624b4e734",
         "ticker": "AAPL", "exchange_code": "US", "mic": "XNAS",
         "figi": "BBG000B9XRY4"},
    ]
    n = load_openfigi.emit_listing_events(log, enriched)
    assert n == 1
    emit.upsert.assert_called_once()
    call = emit.upsert.call_args
    assert call.args[0] == "UpsertListing"
    payload = call.kwargs["payload"]
    assert payload["ticker"] == "AAPL"
    assert payload["company_gmr_id"] == "00040372-dad6-5d34-882c-8b8624b4e734"
    assert payload["isin"] == "US0378331005"
    assert payload["mic"] == "XNAS"


def test_emit_skipped_when_no_enriched_rows():
    log, emit = _mock_log()
    n = load_openfigi.emit_listing_events(log, [])
    assert n == 0
    log.batch.assert_not_called()


def test_query_openfigi_drops_entries_without_ticker():
    """OpenFIGI sometimes returns data with empty ticker — those are
    useless for our keyed-by-ticker schema, so drop."""
    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = [
        {"data": [{"ticker": "AAPL", "exchCode": "US", "micCode": "XNAS",
                   "figi": "BBG000B9XRY4"}]},
        {"data": [{"ticker": "", "exchCode": "??", "figi": "BBG000xxx"}]},
        {"warning": "no match"},
    ]
    with patch.object(load_openfigi.httpx, "post", return_value=fake_resp):
        out = load_openfigi.query_openfigi(["A", "B", "C"])
    assert len(out) == 1
    assert out[0]["isin"] == "A"
    assert out[0]["ticker"] == "AAPL"


def test_query_openfigi_returns_empty_on_http_error():
    with patch.object(
        load_openfigi.httpx, "post",
        side_effect=httpx.HTTPError("boom"),
    ):
        out = load_openfigi.query_openfigi(["A"])
    assert out == []
