"""Tests for load_openfigi event-log migration."""
# Tests reach into the module's mode-specific result shapers (_isin_results,
# _lei_results); they're underscored only because they're not part of the
# public CLI surface, not because they're unsafe.
# pylint: disable=missing-function-docstring,protected-access
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
    log, _emit = _mock_log()
    n = load_openfigi.emit_listing_events(log, [])
    assert n == 0
    log.batch.assert_not_called()


def test_query_openfigi_returns_raw_response():
    """query_openfigi is now idType-agnostic: it just POSTs the
    pre-built payload and returns the raw response. Filtering moved
    to _isin_results / _lei_results."""
    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = [
        {"data": [{"ticker": "AAPL", "exchCode": "US", "micCode": "XNAS",
                   "figi": "BBG000B9XRY4"}]},
        {"warning": "no match"},
    ]
    payload = [
        {"idType": "ID_ISIN", "idValue": "US0378331005"},
        {"idType": "ID_ISIN", "idValue": "XX0000000000"},
    ]
    with patch.object(load_openfigi.httpx, "post", return_value=fake_resp):
        out = load_openfigi.query_openfigi(payload)
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["data"][0]["ticker"] == "AAPL"


def test_query_openfigi_returns_empty_on_http_error():
    payload = [{"idType": "ID_ISIN", "idValue": "A"}]
    with patch.object(
        load_openfigi.httpx, "post",
        side_effect=httpx.HTTPError("boom"),
    ):
        out = load_openfigi.query_openfigi(payload)
    assert out == []


def test_isin_results_drops_entries_without_ticker():
    """OpenFIGI sometimes returns data with empty ticker, or no data
    at all — both are useless for our keyed-by-ticker schema, so drop."""
    response = [
        {"data": [{"ticker": "AAPL", "exchCode": "US", "micCode": "XNAS",
                   "figi": "BBG000B9XRY4"}]},
        {"data": [{"ticker": "", "exchCode": "??", "figi": "BBG000xxx"}]},
        {"warning": "no match"},
    ]
    out = load_openfigi._isin_results(response, ["A", "B", "C"])
    assert len(out) == 1
    assert out[0]["isin"] == "A"
    assert out[0]["ticker"] == "AAPL"
    assert out[0]["mic"] == "XNAS"


def test_lei_results_filters_non_equity_instruments():
    """LEI lookups return bonds, options, warrants too. Only Equity
    and Pref Equity should produce Listings."""
    response = [
        {"data": [
            {"ticker": "SIE", "exchCode": "GR", "micCode": "XETR",
             "figi": "BBG000PRJ717", "marketSector": "Equity"},
            {"ticker": "SIE-BOND", "exchCode": "DE",
             "figi": "BBG000BOND00", "marketSector": "Corp"},
            {"ticker": "SIE.PFD", "exchCode": "GR", "micCode": "XETR",
             "figi": "BBG000PFD000", "marketSector": "Pref Equity"},
        ]},
    ]
    out = load_openfigi._lei_results(response, ["LEI-SIEMENS"])
    tickers = {r["ticker"] for r in out}
    assert tickers == {"SIE", "SIE.PFD"}
    assert all(r["lei"] == "LEI-SIEMENS" for r in out)


def test_lei_results_dedupes_on_ticker_and_exchange():
    """OpenFIGI sometimes lists the same (ticker, exchCode) twice
    (e.g. different composite/local FIGIs). De-dupe before emission."""
    response = [
        {"data": [
            {"ticker": "SAP", "exchCode": "GR", "micCode": "XETR",
             "figi": "BBG000BB1RM2", "marketSector": "Equity"},
            {"ticker": "SAP", "exchCode": "GR", "micCode": "XETR",
             "figi": "BBG000BB1RM3", "marketSector": "Equity"},
            {"ticker": "SAP", "exchCode": "GY", "micCode": "XFRA",
             "figi": "BBG000BB1RM4", "marketSector": "Equity"},
        ]},
    ]
    out = load_openfigi._lei_results(response, ["LEI-SAP"])
    assert len(out) == 2
    assert {(r["ticker"], r["exchange_code"]) for r in out} == {
        ("SAP", "GR"), ("SAP", "GY"),
    }


def test_lei_results_skips_entries_without_data():
    """LEIs of private companies return {warning: 'no match'} or no
    data field — both should be silently skipped."""
    response = [
        {"warning": "no match"},
        {"data": []},
        {"data": [{"ticker": "VOW3", "exchCode": "GR",
                   "marketSector": "Equity"}]},
    ]
    out = load_openfigi._lei_results(response, ["L1", "L2", "L3"])
    assert len(out) == 1
    assert out[0]["lei"] == "L3"
    assert out[0]["ticker"] == "VOW3"
