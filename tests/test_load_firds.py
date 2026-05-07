"""Tests for the event-log FIRDS loader."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.etl import load_firds


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def test_emit_uses_isin_as_ticker():
    """FIRDS only carries ISIN — that's the primary key. OpenFIGI
    emits a separate event with the canonical ticker later."""
    log, emit = _mock_log()
    records = [{
        "isin": "DE0007236101", "instrument_name": "SIEMENS AG",
        "instrument_type": "equity", "cfi_code": "ESVUFR",
        "trading_venue_mic": "XETR", "currency": "EUR",
        "lei": "529900N0AYWGEKMC0739",
    }]
    summary = load_firds.emit_listings(
        log, records,
        {"529900N0AYWGEKMC0739": "00040372-dad6-5d34-882c-8b8624b4e734"},
    )
    assert summary == {"total": 1, "emitted": 1, "skipped": 0}
    payload = emit.upsert.call_args.kwargs["payload"]
    assert payload["ticker"] == "DE0007236101"
    assert payload["isin"] == "DE0007236101"
    assert payload["mic"] == "XETR"
    assert payload["currency"] == "EUR"
    assert payload["company_gmr_id"] == "00040372-dad6-5d34-882c-8b8624b4e734"


def test_skip_records_without_resolvable_lei():
    log, emit = _mock_log()
    records = [
        {"isin": "X1", "lei": "GOOD", "trading_venue_mic": "M",
         "currency": "EUR", "instrument_name": "n",
         "instrument_type": "equity", "cfi_code": "ESVUFR"},
        {"isin": "X2", "lei": "MISSING", "trading_venue_mic": "M",
         "currency": "EUR", "instrument_name": "n",
         "instrument_type": "equity", "cfi_code": "ESVUFR"},
        {"isin": "X3", "lei": None, "trading_venue_mic": "M",
         "currency": "EUR", "instrument_name": "n",
         "instrument_type": "equity", "cfi_code": "ESVUFR"},
    ]
    summary = load_firds.emit_listings(
        log, records, {"GOOD": "00040372-dad6-5d34-882c-8b8624b4e734"},
    )
    assert summary == {"total": 3, "emitted": 1, "skipped": 2}


def test_extract_drops_non_equity_non_fund():
    """CFI codes outside the 'E' or 'C' families (eg debt 'DT', warrant 'RW')
    are dropped — the loader is for cash equities + funds."""
    gnl = _gnl_xml("DE0007236101", "Some Bond", "DT")
    rd = _rd_xml(gnl)
    assert load_firds._extract_instrument(gnl, rd) is None


def test_extract_keeps_equity():
    gnl = _gnl_xml("DE0007236101", "Siemens", "ESVUFR")
    rd = _rd_xml(gnl)
    rec = load_firds._extract_instrument(gnl, rd)
    assert rec is not None
    assert rec["isin"] == "DE0007236101"
    assert rec["instrument_type"] == "equity"


# ── helpers ───────────────────────────────────────────────────────

class _Elem:
    """Minimal stand-in for xml.etree elements — just enough to
    feed _extract_instrument without writing real XML."""

    def __init__(self, tag: str, text: str = "", children: list | None = None):
        self.tag = tag
        self.text = text
        self._children = children or []

    def __iter__(self):
        return iter(self._children)


def _gnl_xml(isin: str, name: str, cfi: str) -> _Elem:
    return _Elem("FinInstrmGnlAttrbts", children=[
        _Elem("Id", isin),
        _Elem("FullNm", name),
        _Elem("ClssfctnTp", cfi),
        _Elem("NtnlCcy", "EUR"),
    ])


def _rd_xml(gnl: _Elem) -> _Elem:
    return _Elem("RefData", children=[
        gnl,
        _Elem("TradgVnRltdAttrbts", children=[_Elem("Id", "XETR")]),
        _Elem("Issr", "529900N0AYWGEKMC0739"),
    ])
