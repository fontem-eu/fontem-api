"""Tests for the event-log FIRDS loader."""
# `_extract_instrument` is the module-internal record-shape parser. The
# leading underscore is the loader saying "don't depend on me from outside";
# this test pins that very shape (so any rewrite of the loader has to keep
# the same record contract), which is the textbook case for protected-access.
# pylint: disable=protected-access
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import pytest

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
    log, _emit = _mock_log()
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
    rd = _record_xml(gnl)
    assert load_firds._extract_instrument(gnl, rd) is None


def test_extract_keeps_equity():
    gnl = _gnl_xml("DE0007236101", "Siemens", "ESVUFR")
    rd = _record_xml(gnl)
    rec = load_firds._extract_instrument(gnl, rd)
    assert rec is not None
    assert rec["isin"] == "DE0007236101"
    assert rec["instrument_type"] == "equity"


def test_extract_marks_terminated_records_inactive():
    """TermntdRcrd → active=False so the consolidator can deactivate
    the Listing; NewRcrd / ModfdRcrd → active=True."""
    gnl = _gnl_xml("DE0007236101", "Siemens", "ESVUFR")
    for wrapper, expected in [
        ("NewRcrd", True),
        ("ModfdRcrd", True),
        ("TermntdRcrd", False),
    ]:
        rec = load_firds._extract_instrument(
            gnl, _record_xml(gnl, wrapper=wrapper), wrapper_tag=wrapper,
        )
        assert rec is not None
        assert rec["active"] is expected, f"{wrapper} should give active={expected}"


# ── Full-XML parser: real DLTINS shape from auth.036.001.03 ───────────


# Five slices lifted verbatim from a 2026-05-27 DLTINS_*_01of02.zip,
# trimmed to the elements the parser inspects (FinInstrmGnlAttrbts,
# Issr, TradgVnRltdAttrbts/Id). Each exercises a different wrapper or
# CFI prefix:
#
#   * ESVUFR ModfdRcrd  → equity, kept, active=True
#   * CEOJMU ModfdRcrd  → collective-investment fund, kept, active=True
#   * ESVUXR NewRcrd    → equity (new listing), kept, active=True
#   * ESVUFR TermntdRcrd → equity, kept, active=False
#   * DEEVRB ModfdRcrd  → debt, dropped by CFI filter
#
# This pins the parser against the actual ESMA schema rather than the
# `<RefData>` element the loader used to (incorrectly) look for. The
# loader had been silently emitting zero events for months until the
# first real-data probe surfaced the gap.
_DLTINS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<BizData xmlns="urn:iso:std:iso:20022:tech:xsd:head.003.001.01">
 <Pyld>
  <Document xmlns="urn:iso:std:iso:20022:tech:xsd:auth.036.001.03">
   <FinInstrmRptgRefDataDltaRpt>
    <FinInstrm><ModfdRcrd>
      <FinInstrmGnlAttrbts>
        <Id>CA37956H1082</Id>
        <FullNm>Global Li-Ion Graphite Corp.</FullNm>
        <ClssfctnTp>ESVUFR</ClssfctnTp>
        <NtnlCcy>CAD</NtnlCcy>
      </FinInstrmGnlAttrbts>
      <Issr>254900AL9ADPBO7BYJ95</Issr>
      <TradgVnRltdAttrbts><Id>STUB</Id></TradgVnRltdAttrbts>
    </ModfdRcrd></FinInstrm>
    <FinInstrm><ModfdRcrd>
      <FinInstrmGnlAttrbts>
        <Id>CA46435V1094</Id>
        <FullNm>iShares Core MSCI Canadian Qua ETFS</FullNm>
        <ClssfctnTp>CEOJMU</ClssfctnTp>
        <NtnlCcy>CAD</NtnlCcy>
      </FinInstrmGnlAttrbts>
      <Issr>549300P8WRDH435O2450</Issr>
      <TradgVnRltdAttrbts><Id>BTFE</Id></TradgVnRltdAttrbts>
    </ModfdRcrd></FinInstrm>
    <FinInstrm><NewRcrd>
      <FinInstrmGnlAttrbts>
        <Id>CA04031A2011</Id>
        <FullNm>Argyle Resources Corp. Registered Shares New o.N.</FullNm>
        <ClssfctnTp>ESVUXR</ClssfctnTp>
        <NtnlCcy>CAD</NtnlCcy>
      </FinInstrmGnlAttrbts>
      <Issr>894500RZ4R5IQCB9U329</Issr>
      <TradgVnRltdAttrbts><Id>HAMB</Id></TradgVnRltdAttrbts>
    </NewRcrd></FinInstrm>
    <FinInstrm><TermntdRcrd>
      <FinInstrmGnlAttrbts>
        <Id>CA04031A1021</Id>
        <FullNm>Argyle Resources Corp.</FullNm>
        <ClssfctnTp>ESVUFR</ClssfctnTp>
        <NtnlCcy>CAD</NtnlCcy>
      </FinInstrmGnlAttrbts>
      <Issr>894500RZ4R5IQCB9U329</Issr>
      <TradgVnRltdAttrbts><Id>BERB</Id></TradgVnRltdAttrbts>
    </TermntdRcrd></FinInstrm>
    <FinInstrm><ModfdRcrd>
      <FinInstrmGnlAttrbts>
        <Id>CH1143296924</Id>
        <FullNm>LQ EXP AZIMUT HOLDING/RWE/STEL 60 111126</FullNm>
        <ClssfctnTp>DEEVRB</ClssfctnTp>
        <NtnlCcy>EUR</NtnlCcy>
      </FinInstrmGnlAttrbts>
      <Issr>ML61HP3A4MKTTA1ZB671</Issr>
      <TradgVnRltdAttrbts><Id>SEDX</Id></TradgVnRltdAttrbts>
    </ModfdRcrd></FinInstrm>
   </FinInstrmRptgRefDataDltaRpt>
  </Document>
 </Pyld>
</BizData>
"""


def test_record_wrappers_cover_new_modified_terminated():
    """auth.036.001.03 (DLTINS) wraps each instrument in exactly one of
    these three elements. The parser must accept all three — looking
    for ``RefData`` (a different ESMA schema) silently drops 100% of
    real records, which is how this bug shipped for months."""
    assert load_firds._RECORD_WRAPPERS == {"NewRcrd", "ModfdRcrd", "TermntdRcrd"}


def test_parse_firds_xml_yields_records_from_real_dltins_shape():
    """End-to-end parser test against a verbatim auth.036.001.03 slice
    (5 instruments: 4 equity/fund + 1 debt). Pins:

      * all three wrapper tags are walked,
      * the equity/fund CFI filter passes ESVUFR, CEOJMU, ESVUXR,
      * the filter drops DEEVRB (debt),
      * TermntdRcrd flips active to False,
      * Issr LEI and TradgVnRltdAttrbts/Id come through verbatim.
    """
    records = list(load_firds.parse_firds_xml(io.BytesIO(_DLTINS_FIXTURE.encode())))
    assert len(records) == 4, [r["isin"] for r in records]

    by_isin = {r["isin"]: r for r in records}

    assert set(by_isin) == {
        "CA37956H1082",   # ESVUFR equity, modified
        "CA46435V1094",   # CEOJMU fund, modified
        "CA04031A2011",   # ESVUXR equity, new listing
        "CA04031A1021",   # ESVUFR equity, terminated
    }
    # Debt was correctly dropped.
    assert "CH1143296924" not in by_isin

    eq = by_isin["CA37956H1082"]
    assert eq["lei"] == "254900AL9ADPBO7BYJ95"
    assert eq["cfi_code"] == "ESVUFR"
    assert eq["instrument_type"] == "equity"
    assert eq["trading_venue_mic"] == "STUB"
    assert eq["active"] is True

    fund = by_isin["CA46435V1094"]
    assert fund["instrument_type"] == "fund"
    assert fund["active"] is True

    new = by_isin["CA04031A2011"]
    assert new["active"] is True   # NewRcrd → active

    term = by_isin["CA04031A1021"]
    assert term["active"] is False  # TermntdRcrd → inactive


def _zip_fixture(xml_bytes: bytes) -> io.BytesIO:
    """Wrap the DLTINS fixture XML in a single-entry zip so the test
    can exercise _load_from_file (the path used by --file mode and
    also the inner loop of --since/Solr mode)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("DLTINS_20260527_01of01.xml", xml_bytes)
    buf.seek(0)
    return buf


def test_load_from_file_emits_one_listing_per_resolvable_lei(tmp_path):
    """End-to-end --file mode against the real DLTINS fixture: 4 equity/
    fund records survive parsing, the LEI resolver knows about 2 of
    their 3 unique issuers, so we expect 3 emitted (2 modified equity +
    1 fund) + 1 skipped (the NewRcrd whose LEI isn't in our fixture
    resolver) + the debt record dropped at parse time."""
    log, emit = _mock_log()
    fixture_zip = tmp_path / "DLTINS.zip"
    fixture_zip.write_bytes(_zip_fixture(_DLTINS_FIXTURE.encode()).getvalue())

    # Resolver knows two issuers; the third (894500RZ4R5IQCB9U329 on
    # the NewRcrd + TermntdRcrd Argyle records) is intentionally
    # absent so we exercise the skip path on real-shape records.
    driver = MagicMock()
    session = MagicMock()
    session.run.return_value = iter([
        {"lei": "254900AL9ADPBO7BYJ95", "gmr_id": "gmr-canada-lithium"},
        {"lei": "549300P8WRDH435O2450", "gmr_id": "gmr-ishares-canada"},
    ])
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)

    summary = load_firds._load_from_file(driver, log, str(fixture_zip))

    assert summary["fin_instrm_seen"] == 5  # full <FinInstrm> count
    assert summary["records_yielded"] == 4  # debt filtered, 4 kept
    assert summary["files_processed"] == 1
    assert summary["total"] == 4
    assert summary["emitted"] == 2  # 2 records resolved (one LEI used twice)
    assert summary["skipped"] == 2

    emitted_isins = {c.kwargs["iri"].rsplit("/", 1)[-1]
                     for c in emit.upsert.call_args_list}
    # The two ModfdRcrd-with-resolvable-LEI records: equity + fund.
    assert emitted_isins == {"CA37956H1082", "CA46435V1094"}


def test_load_from_file_raises_when_parser_yields_nothing(tmp_path):
    """The wrapper-tag bug used to look like a clean "success" run.
    The FirdsParseError guard makes the cronjob fail instead: if a zip
    contained <FinInstrm> elements but the parser yielded zero records,
    something about the schema or filter is wrong and we want to know.
    """
    # Use the old (buggy) wrapper tag <RefData> instead of NewRcrd/Mod/Term —
    # the parser will count FinInstrm elements but yield nothing.
    bad_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <BizData>
      <FinInstrm><RefData>
        <FinInstrmGnlAttrbts><Id>DE0007236101</Id>
          <ClssfctnTp>ESVUFR</ClssfctnTp></FinInstrmGnlAttrbts>
        <Issr>529900N0AYWGEKMC0739</Issr>
      </RefData></FinInstrm>
    </BizData>"""
    bad_zip = tmp_path / "bad.zip"
    bad_zip.write_bytes(_zip_fixture(bad_xml).getvalue())

    log, _emit = _mock_log()
    driver = MagicMock()

    with pytest.raises(load_firds.FirdsParseError):
        load_firds._load_from_file(driver, log, str(bad_zip))


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


def _record_xml(gnl: _Elem, wrapper: str = "ModfdRcrd") -> _Elem:
    """Build a fake DLTINS record wrapper (NewRcrd / ModfdRcrd /
    TermntdRcrd) containing the given FinInstrmGnlAttrbts + issuer +
    trading-venue children. Used to drive _extract_instrument in
    isolation without writing real XML."""
    return _Elem(wrapper, children=[
        gnl,
        _Elem("TradgVnRltdAttrbts", children=[_Elem("Id", "XETR")]),
        _Elem("Issr", "529900N0AYWGEKMC0739"),
    ])


# ── Disk cache (DLTINS zips are immutable) ────────────────────────


def test_download_zip_writes_cache_on_miss_and_reads_on_hit(tmp_path, monkeypatch):
    """First call hits the network and writes the cache; second call
    returns from disk without touching the network."""
    monkeypatch.setattr(load_firds, "_FIRDS_CACHE_DIR", str(tmp_path))

    body = b"PK\x03\x04not-a-real-zip"
    calls = {"n": 0}

    def fake_get_with_retry(_url, **_kw):
        calls["n"] += 1
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.content = body
        return resp

    monkeypatch.setattr(load_firds, "get_with_retry", fake_get_with_retry)
    url = "https://firds.esma.europa.eu/firds/DLTINS_20260527_01of01.zip"

    # Miss → network + cache write.
    buf = load_firds.download_zip(url)
    assert buf is not None and buf.getvalue() == body
    assert calls["n"] == 1
    cached = tmp_path / "DLTINS_20260527_01of01.zip"
    assert cached.read_bytes() == body
    assert not (tmp_path / "DLTINS_20260527_01of01.zip.partial").exists()

    # Hit → cache read, no network.
    buf2 = load_firds.download_zip(url)
    assert buf2 is not None and buf2.getvalue() == body
    assert calls["n"] == 1, "second call must NOT re-fetch on cache hit"


def test_download_zip_no_cache_when_dir_unset(tmp_path, monkeypatch):
    """Empty FIRDS_CACHE_DIR (default) → behave as before, no disk I/O."""
    monkeypatch.setattr(load_firds, "_FIRDS_CACHE_DIR", "")

    body = b"PK\x03\x04"

    def fake_get_with_retry(_url, **_kw):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.content = body
        return resp

    monkeypatch.setattr(load_firds, "get_with_retry", fake_get_with_retry)
    load_firds.download_zip("https://firds.esma.europa.eu/firds/x.zip")
    # The cache dir wasn't created and no file lands in tmp_path.
    assert not list(tmp_path.iterdir())


def test_cache_path_rejects_url_filenames_with_path_chars():
    """Defence in depth: a URL whose tail contains "/" or "\\" must not
    escape FIRDS_CACHE_DIR. _cache_path_for returns None in that case.
    """
    # The slash inside the path here is the URL separator, so basename
    # extraction picks up an empty string and bails.
    assert load_firds._cache_path_for("https://x/") is None


# ── ESMA throttling wiring ─────────────────────────────────────────


def test_module_imports_rate_limiter_and_constants():
    """ESMA Solr + the FIRDS delta-zip CDN are behind an Azure App
    Gateway that drops TLS handshakes after bursts. The loader must
    use a RateLimiter + the longer retry budget so transient WAF
    blocks don't fail the whole run.

    This is structural — every change to FIRDS upstream-call sites
    must keep the rate limiter wired in.
    """
    from src.etl._http_retry import RateLimiter  # pylint: disable=import-outside-toplevel
    assert isinstance(load_firds._firds_limiter, RateLimiter)
    # Default 6 req/min → 10 s between requests.
    assert load_firds._firds_limiter.min_interval_s == 10.0
    # Longer than the loader-default 3 attempts.
    assert load_firds._FIRDS_MAX_ATTEMPTS >= 5
    # Longer than the loader-default 5 s base.
    assert load_firds._FIRDS_BASE_DELAY_S >= 10.0


def test_query_firds_files_passes_rate_limiter_to_get_with_retry(monkeypatch):
    """Both FIRDS upstream calls go through get_with_retry; they MUST
    pass the module-level _firds_limiter so retries are governed."""
    captured: dict = {}

    def fake_get_with_retry(url, **kwargs):  # pylint: disable=unused-argument
        captured["url"] = url
        captured["kwargs"] = kwargs
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"response": {"docs": []}}
        return resp

    monkeypatch.setattr(load_firds, "get_with_retry", fake_get_with_retry)
    load_firds.query_firds_files("2026-05-01")
    assert captured["kwargs"]["rate_limiter"] is load_firds._firds_limiter
    assert captured["kwargs"]["max_attempts"] == load_firds._FIRDS_MAX_ATTEMPTS
    assert captured["kwargs"]["base_delay"] == load_firds._FIRDS_BASE_DELAY_S


def test_download_zip_passes_rate_limiter_to_get_with_retry(monkeypatch):
    captured: dict = {}

    def fake_get_with_retry(url, **kwargs):  # pylint: disable=unused-argument
        captured["url"] = url
        captured["kwargs"] = kwargs
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.content = b"PK\x03\x04fake"
        return resp

    monkeypatch.setattr(load_firds, "get_with_retry", fake_get_with_retry)
    load_firds.download_zip("https://firds.esma.europa.eu/firds/x.zip")
    assert captured["kwargs"]["rate_limiter"] is load_firds._firds_limiter
    assert captured["kwargs"]["max_attempts"] == load_firds._FIRDS_MAX_ATTEMPTS
    assert captured["kwargs"]["base_delay"] == load_firds._FIRDS_BASE_DELAY_S
