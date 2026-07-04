"""Tests for load_openfigi event-log migration."""
# pylint: disable=too-many-lines
# Tests reach into the module's mode-specific result shapers (_isin_results,
# _lei_results); they're underscored only because they're not part of the
# public CLI surface, not because they're unsafe.
# pylint: disable=missing-function-docstring,protected-access
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.etl import _gleif_isin_bulk, load_openfigi


@pytest.fixture(autouse=True)
def _stub_gleif_bulk(monkeypatch):
    """Don't hit the GLEIF bulk API during unit tests. Default returns
    an empty mapping; the LEI-mode runner then falls through to the
    "no canonicals" branch unless the test explicitly overrides this
    fixture with a richer mapping (see bulk-path tests below)."""
    def _empty(target_leis=None, cache_dir=None, http_client=None):
        # Signature matches load_isin_mapping; we accept and discard.
        del target_leis, cache_dir, http_client
        return {}
    monkeypatch.setattr(
        _gleif_isin_bulk, "load_isin_mapping", _empty,
    )


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
             "figi": "BBG000PRJ717", "marketSector": "Equity", "securityType2": "Common Stock"},
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
             "figi": "BBG000BB1RM2", "marketSector": "Equity", "securityType2": "Common Stock"},
            {"ticker": "SAP", "exchCode": "GR", "micCode": "XETR",
             "figi": "BBG000BB1RM3", "marketSector": "Equity", "securityType2": "Common Stock"},
            {"ticker": "SAP", "exchCode": "GY", "micCode": "XFRA",
             "figi": "BBG000BB1RM4", "marketSector": "Equity", "securityType2": "Common Stock"},
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
                   "marketSector": "Equity", "securityType2": "Common Stock"}]},
    ]
    out = load_openfigi._lei_results(response, ["L1", "L2", "L3"])
    assert len(out) == 1
    assert out[0]["lei"] == "L3"
    assert out[0]["ticker"] == "VOW3"


# ── per-tier batch sizing ─────────────────────────────────────────


def test_api_limits_keyed_tier():
    """With an API key OpenFIGI accepts up to 100 IDs per request and
    25 req per 6 s. Our defaults: batch=100, sleep=0.30 s."""
    batch, sleep_s = load_openfigi._api_limits("X-OPENFIGI-FAKE-KEY")
    assert batch == 100
    assert sleep_s == 0.30


def test_api_limits_anonymous_tier():
    """Keyless tier caps at 10 IDs per request and 25 req per minute.
    Sending 100 in one request (the previous hard-coded value) returned
    HTTP 413 from every batch in the staging run. We now cap at 10 +
    sleep 3 s when no key is set so the loader still makes progress
    instead of producing zero enriched listings.
    """
    batch, sleep_s = load_openfigi._api_limits(None)
    assert batch == 10
    assert sleep_s == 3.0
    # Empty string is also "no key" (env var unset → "").
    assert load_openfigi._api_limits("") == (10, 3.0)


def test_run_mode_uses_anonymous_batch_size_when_no_key(monkeypatch):
    """Regression: the loader used to always pass batches of 100,
    which OpenFIGI rejects without an API key (HTTP 413). _run_mode
    must now consult _api_limits and chunk to 10 when key is absent.
    """
    sent_batch_sizes: list[int] = []

    def fake_process_batch(_cfg, batch, _id_to_company, _api_key):
        sent_batch_sizes.append(len(batch))
        return []  # treat as "no matches"

    monkeypatch.setattr(load_openfigi, "_process_batch", fake_process_batch)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)

    driver = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(
        return_value=MagicMock(
            run=lambda *a, **kw: [
                {"isin": f"ISIN{i:08d}", "company_gmr_id": f"g{i}"}
                for i in range(25)
            ],
        ),
    )
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    log, _emit = _mock_log()

    summary = load_openfigi._run_mode("isin", driver, log, limit=25, api_key=None)
    # 25 IDs / 10 per batch → 3 batches of 10, 10, 5
    assert sent_batch_sizes == [10, 10, 5]
    assert summary["queried"] == 25


# ── lei-reeval mode ───────────────────────────────────────────────


def test_retires_for_suspects_keeps_canonical_tickers_alone():
    # The suspect already matches a canonical (the LEI lookup
    # produced the same ticker). Nothing to retire.
    rows = [{"lei": "L1", "company_gmr_id": "g1",
             "suspect_tickers": ["SAP.GR"]}]
    enriched = [{"lei": "L1", "ticker": "SAP.GR", "exchange_code": "GR",
                 "company_gmr_id": "g1"}]
    assert not load_openfigi._retires_for_suspects(rows, enriched)


def test_retires_for_suspects_picks_replacement_by_exchange_suffix():
    # The MOTA.LS / EGL.LS case: suspect ticker has a Portuguese
    # ".LS" suffix; OpenFIGI returns multiple venues. The retire
    # record's replacement should be the one on the same exchange.
    rows = [{"lei": "MOTA-LEI", "company_gmr_id": "g1",
             "suspect_tickers": ["MOTA.LS"]}]
    enriched = [
        {"lei": "MOTA-LEI", "ticker": "EGL.LS", "exchange_code": "LS",
         "company_gmr_id": "g1"},
        {"lei": "MOTA-LEI", "ticker": "EGL.OTC", "exchange_code": "OTC",
         "company_gmr_id": "g1"},
    ]
    retires = load_openfigi._retires_for_suspects(rows, enriched)
    assert len(retires) == 1
    assert retires[0] == {
        "ticker": "MOTA.LS",
        "company_gmr_id": "g1",
        "replacement_ticker": "EGL.LS",
    }


def test_pick_replacement_matches_bare_symbol_against_canonical():
    # The GSK.L case: legacy fabricator minted "GSK.L" because the
    # first word of "GLAXOSMITHKLINE PLC" happened to be GSK; OpenFIGI
    # returns ticker="GSK" exchCode="LN" for the same Company. The
    # bare-symbol match must produce an AssertSameAs(GSK.L -> GSK)
    # rather than retiring GSK.L with no replacement.
    canon = [
        {"ticker": "GSK", "exchange_code": "LN"},
        {"ticker": "GS71", "exchange_code": "GR"},
        {"ticker": "GLAXF", "exchange_code": "US"},
    ]
    assert load_openfigi._pick_replacement("GSK.L", canon) == "GSK"


def test_pick_replacement_uses_alias_when_bare_misses():
    # Suspect "ACME.L" doesn't match a bare canonical, but the
    # exchange-code alias does: "LN" -> "L" via _EXCH_ALIASES.
    canon = [{"ticker": "GSK", "exchange_code": "LN"}]
    assert load_openfigi._pick_replacement("ACME.L", canon) == "GSK"


def test_pick_replacement_uses_alias_for_lisbon_pl_to_ls():
    # The Mota-Engil case once witness ISIN arrives: suspect
    # "MOTA.LS" against canonical "EGL" on exchCode "PL". The alias
    # "PL" -> "LS" should pin the venue and emit AssertSameAs.
    canon = [
        {"ticker": "EGL", "exchange_code": "PL"},
        {"ticker": "M09", "exchange_code": "GR"},
    ]
    assert load_openfigi._pick_replacement("MOTA.LS", canon) == "EGL"


def test_retires_for_suspects_falls_back_to_sole_canonical():
    # No exchange-suffix match but only one canonical exists — that's
    # still our best guess, so the AssertSameAs should target it.
    rows = [{"lei": "L1", "company_gmr_id": "g1",
             "suspect_tickers": ["ACME.XX"]}]
    enriched = [{"lei": "L1", "ticker": "ACME.YY", "exchange_code": "YY",
                 "company_gmr_id": "g1"}]
    retires = load_openfigi._retires_for_suspects(rows, enriched)
    assert retires[0]["replacement_ticker"] == "ACME.YY"


def test_retires_for_suspects_skips_when_no_replacement():
    # ASHTEAD.L observed case: OpenFIGI returned only US ADRs and
    # German listings for the FTSE 100 issuer, no London entry.
    # The legitimate suspect ASHTEAD.L has no bare-symbol match
    # (canonicals are ASHGY, ASHTY, ...) and no suffix match
    # (no canonical on LN). We have no evidence the suspect is
    # wrong — conservatively leave it alone, do not emit a retire
    # record at all.
    rows = [{"lei": "L1", "company_gmr_id": "g1",
             "suspect_tickers": ["ASHTEAD.L"]}]
    enriched = [
        {"lei": "L1", "ticker": "ASHGY", "exchange_code": "US",
         "company_gmr_id": "g1"},
        {"lei": "L1", "ticker": "ASHTY", "exchange_code": "UV",
         "company_gmr_id": "g1"},
    ]
    retires = load_openfigi._retires_for_suspects(rows, enriched)
    assert not retires


def test_retires_for_suspects_no_canonicals_means_no_retire_records():
    # OpenFIGI returned nothing for this LEI (private company, rate
    # limit, lookup miss). Don't touch existing Listings — we have
    # no evidence they're wrong. Empty `retires` list is the
    # whole-batch safety net inside _retires_for_suspects when
    # canon == [].
    rows = [{"lei": "L1", "company_gmr_id": "g1",
             "suspect_tickers": ["SOMETHING.LS"]}]
    retires = load_openfigi._retires_for_suspects(rows, enriched=[])
    assert not retires


def test_emit_retire_events_emits_upsert_inactive_plus_same_as():
    log, emit = _mock_log()
    retires = [{
        "ticker": "MOTA.LS", "company_gmr_id": "g1",
        "replacement_ticker": "EGL.LS",
    }]
    n = load_openfigi.emit_retire_events(log, retires)
    # Two emit.upsert calls: one UpsertListing(active=False) +
    # one AssertSameAs envelope.
    assert n == 2
    assert emit.upsert.call_count == 2
    upsert_listing_kwargs = emit.upsert.call_args_list[0].kwargs
    assert upsert_listing_kwargs["payload"]["active"] is False
    assert upsert_listing_kwargs["payload"]["ticker"] == "MOTA.LS"
    same_as_kwargs = emit.upsert.call_args_list[1].kwargs
    assert same_as_kwargs["payload"]["a_iri"].endswith("/MOTA.LS")
    assert same_as_kwargs["payload"]["b_iri"].endswith("/EGL.LS")
    assert same_as_kwargs["payload"]["method"] == "openfigi_lei_reeval"
    # AssertSameAs envelope is emit.upsert("AssertSameAs", ...) — the
    # event_type pinning is what keeps consumers from mistaking it
    # for an UpsertListing.
    assert emit.upsert.call_args_list[1].args[0] == "AssertSameAs"


def test_emit_retire_events_skips_same_as_when_replacement_unknown():
    log, emit = _mock_log()
    retires = [{
        "ticker": "WEIRD.XX", "company_gmr_id": "g1",
        "replacement_ticker": None,
    }]
    n = load_openfigi.emit_retire_events(log, retires)
    assert n == 1  # just the deactivation; no AssertSameAs envelope
    assert emit.upsert.call_count == 1


def test_lei_reeval_mode_runs_retire_step():
    # End-to-end shape check: the lei-reeval mode's _MODES entry
    # carries the retire_suspects marker, the via_lei dispatch flag,
    # and uses the suspect-Listing selector.
    cfg = load_openfigi._MODES["lei-reeval"]
    assert cfg["fetch"] is load_openfigi.fetch_leis_with_suspect_listings
    assert cfg["retire_suspects"] is True
    assert cfg["via_lei"] is True
    # The broken ID_LEI path must not come back — assert the field
    # is absent so a future refactor doesn't silently reintroduce it.
    assert "id_type" not in cfg


def test_lei_mode_routes_through_via_lei_path():
    # Same guard for the LEI-no-Listing mode. Used to query ID_LEI
    # directly which OpenFIGI rejects with body
    # `[{"error":"Invalid value for idType."}]` — see memory:
    # openfigi-no-id-lei.
    cfg = load_openfigi._MODES["lei"]
    assert cfg["fetch"] is load_openfigi.fetch_leis_no_listing
    assert cfg["via_lei"] is True
    assert "id_type" not in cfg


# ── GLEIF helper ──────────────────────────────────────────────────


def test_gleif_get_isins_returns_attribute_values():
    """GLEIF returns ``{data: [{attributes: {isin: ...}}, ...]}``;
    the helper just walks that and strips the wrapper."""
    fake = MagicMock(spec=httpx.Response)
    fake.status_code = 200
    fake.raise_for_status.return_value = None
    fake.json.return_value = {
        "data": [
            {"attributes": {"isin": "PTMENYOM0005"}},
            {"attributes": {"isin": "PTMENZOM0004"}},
            {"attributes": {}},
        ],
    }
    with patch.object(load_openfigi.httpx, "get", return_value=fake):
        out = load_openfigi.gleif_get_isins("549300L6RR1203WN9F57")
    assert out == ["PTMENYOM0005", "PTMENZOM0004"]


def test_gleif_get_isins_returns_empty_on_404():
    fake = MagicMock(spec=httpx.Response)
    fake.status_code = 404
    fake.raise_for_status.side_effect = AssertionError(
        "should not raise on 404",
    )
    with patch.object(load_openfigi.httpx, "get", return_value=fake):
        out = load_openfigi.gleif_get_isins("UNKNOWN")
    assert out == []


def test_gleif_get_isins_returns_empty_on_http_error():
    with patch.object(
        load_openfigi.httpx, "get",
        side_effect=httpx.HTTPError("boom"),
    ):
        out = load_openfigi.gleif_get_isins("L1")
    assert out == []


# ── equity reshape ────────────────────────────────────────────────


def test_equity_canonicals_filters_bonds_and_attaches_isin():
    # OpenFIGI returns the input ISIN positionally — the reshape must
    # zip(response, isins) to preserve the binding. Only equity-sector
    # instruments pass through.
    response = [
        {"data": [
            {"ticker": "EGL", "exchCode": "PL",
             "marketSector": "Equity", "securityType2": "Common Stock", "micCode": "XLIS",
             "figi": "BBG000BV96Y8"},
        ]},
        {"data": [
            {"ticker": "EGLPL 4.25 12/02/26",
             "exchCode": "EURONEXT-LISBON",
             "marketSector": "Corp", "figi": "BBG013KN1016"},
        ]},
    ]
    out = load_openfigi._equity_canonicals_from_response(
        response, ["PTMEN0AE0005", "PTMENYOM0005"],
        lei="LEI", company_gmr_id="gid",
    )
    assert len(out) == 1
    assert out[0]["ticker"] == "EGL"
    assert out[0]["exchange_code"] == "PL"
    assert out[0]["isin"] == "PTMEN0AE0005"
    assert out[0]["lei"] == "LEI"
    assert out[0]["company_gmr_id"] == "gid"


def test_equity_canonicals_dedupes_across_isins_of_same_lei():
    response = [
        {"data": [
            {"ticker": "EGL", "exchCode": "PL",
             "marketSector": "Equity", "securityType2": "Common Stock", "figi": "F1"},
        ]},
        {"data": [
            {"ticker": "EGL", "exchCode": "PL",
             "marketSector": "Equity", "securityType2": "Common Stock", "figi": "F2"},
        ]},
    ]
    out = load_openfigi._equity_canonicals_from_response(
        response, ["ISIN1", "ISIN2"],
        lei="LEI", company_gmr_id="gid",
    )
    assert len(out) == 1
    assert out[0]["isin"] == "ISIN1"


# ── _resolve_lei_to_canonicals: witness vs gleif ──────────────────


def test_resolve_uses_witness_isins_when_present(monkeypatch):
    calls = {"openfigi": 0, "gleif": 0}

    def fake_query_openfigi(_payload, _api_key):
        calls["openfigi"] += 1
        return [{"data": [{"ticker": "EGL", "exchCode": "PL",
                           "marketSector": "Equity", "securityType2": "Common Stock"}]}]

    def fake_gleif(_lei, client=None):  # pragma: no cover  # pylint: disable=unused-argument
        calls["gleif"] += 1
        return []

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi, "gleif_get_isins", fake_gleif)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)

    row = {"lei": "L1", "company_gmr_id": "g1",
           "witness_isins": ["PTMEN0AE0005"], "suspect_tickers": []}
    canonicals, source, _unknown = load_openfigi._resolve_lei_to_canonicals(
        row, batch_size=10, api_key=None,
    )
    assert source == "witness"
    assert calls["gleif"] == 0
    assert len(canonicals) == 1
    assert canonicals[0]["ticker"] == "EGL"


def test_resolve_falls_back_to_gleif_when_no_witness(monkeypatch):
    calls = {"openfigi": 0, "gleif": 0}

    def fake_query_openfigi(_payload, _api_key):
        calls["openfigi"] += 1
        return [{"data": [{"ticker": "ACME", "exchCode": "XX",
                           "marketSector": "Equity", "securityType2": "Common Stock"}]}]

    def fake_gleif(_lei, client=None):  # pylint: disable=unused-argument
        calls["gleif"] += 1
        return ["FAKEISIN1"]

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi, "gleif_get_isins", fake_gleif)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)

    row = {"lei": "L1", "company_gmr_id": "g1",
           "witness_isins": [], "suspect_tickers": []}
    canonicals, source, _unknown = load_openfigi._resolve_lei_to_canonicals(
        row, batch_size=10, api_key=None,
    )
    assert source == "gleif"
    assert calls["gleif"] == 1
    assert len(canonicals) == 1


def test_resolve_returns_none_source_when_no_isins(monkeypatch):
    monkeypatch.setattr(load_openfigi, "gleif_get_isins",
                        lambda _lei, client=None: [])
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    row = {"lei": "L1", "company_gmr_id": "g1",
           "witness_isins": [], "suspect_tickers": []}
    canonicals, source, _unknown = load_openfigi._resolve_lei_to_canonicals(
        row, batch_size=10, api_key=None,
    )
    assert not canonicals
    assert source == "none"


def test_resolve_chunks_large_isin_sets(monkeypatch):
    # Mota's LEI returned 15 ISINs from GLEIF. Anonymous tier batch
    # is 10 — the resolver must split into 2 OpenFIGI calls (10 + 5),
    # not one call of 15 (which would 413).
    sent_batch_sizes: list[int] = []

    def fake_query_openfigi(payload, _api_key):
        sent_batch_sizes.append(len(payload))
        return [{"data": []}] * len(payload)

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)

    row = {"lei": "L1", "company_gmr_id": "g1",
           "witness_isins": [f"ISIN{i:04d}" for i in range(15)],
           "suspect_tickers": []}
    load_openfigi._resolve_lei_to_canonicals(
        row, batch_size=10, api_key=None,
    )
    assert sent_batch_sizes == [10, 5]


# ── _run_mode_via_lei end-to-end ──────────────────────────────────


def test_run_mode_via_lei_emits_canonicals_and_retires_suspect(monkeypatch):
    # Mota-style happy path: one Company has a fabricated MOTA.LS
    # Listing AND a sibling FIRDS-emitted ISIN (the witness). OpenFIGI
    # returns EGL on the Lisbon venue. Expect one canonical
    # UpsertListing (EGL), one retire UpsertListing(MOTA.LS,
    # active=False), and one AssertSameAs(MOTA.LS -> EGL).
    rows = [{
        "lei": "549300L6RR1203WN9F57",
        "company_gmr_id": "g1",
        "suspect_tickers": ["MOTA.LS"],
        "witness_isins": ["PTMEN0AE0005"],
    }]
    monkeypatch.setitem(
        load_openfigi._MODES["lei-reeval"],
        "fetch", lambda _driver, _limit: rows,
    )

    def fake_query_openfigi(_payload, _api_key):
        return [{"data": [
            {"ticker": "EGL", "exchCode": "LS",
             "marketSector": "Equity", "securityType2": "Common Stock", "micCode": "XLIS"},
        ]}]

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi, "gleif_get_isins",
                        lambda _lei, client=None: [])
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)

    log, emit = _mock_log()
    summary = load_openfigi._run_mode_via_lei(
        "lei-reeval", driver=MagicMock(), log=log,
        limit=1, api_key=None,
    )
    assert summary["queried"] == 1
    assert summary["enriched"] == 1
    # Three emit.upsert envelopes in order: canonical, retire,
    # AssertSameAs.
    assert emit.upsert.call_count == 3
    call_types = [c.args[0] for c in emit.upsert.call_args_list]
    assert call_types == [
        "UpsertListing", "UpsertListing", "AssertSameAs",
    ]


def test_run_mode_via_lei_no_isins_no_emissions(monkeypatch):
    # Company with LEI but no witness, GLEIF empty (private). Should
    # produce zero canonicals and not retire anything. Better a stale
    # Listing than a wrongly-deactivated one when we have no evidence.
    rows = [{"lei": "L1", "company_gmr_id": "g1",
             "suspect_tickers": ["WEIRD.XX"], "witness_isins": []}]
    monkeypatch.setitem(
        load_openfigi._MODES["lei-reeval"],
        "fetch", lambda _driver, _limit: rows,
    )
    monkeypatch.setattr(load_openfigi, "gleif_get_isins",
                        lambda _lei, client=None: [])
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    log, emit = _mock_log()
    summary = load_openfigi._run_mode_via_lei(
        "lei-reeval", driver=MagicMock(), log=log,
        limit=1, api_key=None,
    )
    assert summary["enriched"] == 0
    assert emit.upsert.call_count == 0


# ── Per-LEI / per-batch commit boundary ───────────────────────────


def test_run_mode_via_lei_opens_one_batch_per_lei_with_canonicals(monkeypatch):
    """Per-LEI commit boundary: each LEI whose OpenFIGI lookup yields
    canonicals gets its OWN ``log.batch(...)`` open + close, so events
    land in the sinks as soon as the LEI is resolved instead of being
    held in memory until the end of the LEI loop.

    Two LEIs in, both with canonicals → exactly two batches.

    This pins the same anti-batching property the TED loader needed
    for the same reason: bulk runs were holding nine hours of in-memory
    work in one transaction, invisible to sinks and lost on restart."""
    rows = [
        {"lei": "L1", "company_gmr_id": "g1", "witness_isins": ["I1"]},
        {"lei": "L2", "company_gmr_id": "g2", "witness_isins": ["I2"]},
    ]
    monkeypatch.setitem(
        load_openfigi._MODES["lei"],
        "fetch", lambda _driver, _limit: rows,
    )

    def fake_query_openfigi(payload, _api_key):
        # Distinct ticker per ISIN so each LEI gets a real canonical.
        out = []
        for entry in payload:
            isin = entry["idValue"]
            out.append({"data": [{
                "ticker": f"T-{isin}", "exchCode": "LS",
                "marketSector": "Equity", "securityType2": "Common Stock", "micCode": "XLIS",
            }]})
        return out

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    log, emit = _mock_log()
    summary = load_openfigi._run_mode_via_lei(
        "lei", driver=MagicMock(), log=log, limit=2, api_key=None,
    )
    assert summary["enriched"] == 2
    assert summary["emitted"] == 2
    assert log.batch.call_count == 2, (
        f"expected 2 per-LEI batches, got {log.batch.call_count}"
    )
    assert emit.upsert.call_count == 2


def test_run_mode_via_lei_skips_batch_open_for_lei_with_no_canonicals(monkeypatch):
    """Per-LEI commit boundary: a LEI whose OpenFIGI lookup yields zero
    canonicals must NOT open a no-op log.batch. Three rows, only the
    middle one has witness ISINs → exactly one batch (for the canonical
    that did land)."""
    rows = [
        {"lei": "L1", "company_gmr_id": "g1", "witness_isins": []},
        {"lei": "L2", "company_gmr_id": "g2", "witness_isins": ["I2"]},
        {"lei": "L3", "company_gmr_id": "g3", "witness_isins": []},
    ]
    monkeypatch.setitem(
        load_openfigi._MODES["lei"],
        "fetch", lambda _driver, _limit: rows,
    )
    monkeypatch.setattr(load_openfigi, "gleif_get_isins",
                        lambda _lei, client=None: [])

    def fake_query_openfigi(payload, _api_key):
        return [{"data": [{
            "ticker": "T-I2", "exchCode": "LS",
            "marketSector": "Equity", "securityType2": "Common Stock", "micCode": "XLIS",
        }]} for _ in payload]

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    log, _emit = _mock_log()
    summary = load_openfigi._run_mode_via_lei(
        "lei", driver=MagicMock(), log=log, limit=3, api_key=None,
    )
    assert summary["enriched"] == 1
    assert log.batch.call_count == 1


def test_run_mode_via_lei_reeval_emits_listing_batch_and_retire_batch_per_lei(
    monkeypatch,
):
    """In lei-reeval mode, each LEI gets up to two batches per row: one
    for the canonical UpsertListing, one for the retire (UpsertListing
    inactive + AssertSameAs). Two LEIs each with a canonical and a
    suspect to retire → 4 batches total (2 listing + 2 retire),
    interleaved per-LEI (not all-listings-first-then-all-retires)."""
    rows = [
        {"lei": "L1", "company_gmr_id": "g1",
         "suspect_tickers": ["MOTA.LS"], "witness_isins": ["I1"]},
        {"lei": "L2", "company_gmr_id": "g2",
         "suspect_tickers": ["ACME.LS"], "witness_isins": ["I2"]},
    ]
    monkeypatch.setitem(
        load_openfigi._MODES["lei-reeval"],
        "fetch", lambda _driver, _limit: rows,
    )

    def fake_query_openfigi(payload, _api_key):
        # One canonical per ISIN so each row's retire has a replacement.
        return [{"data": [{
            "ticker": f"CANON-{entry['idValue']}", "exchCode": "LS",
            "marketSector": "Equity", "securityType2": "Common Stock", "micCode": "XLIS",
        }]} for entry in payload]

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    log, _emit = _mock_log()
    summary = load_openfigi._run_mode_via_lei(
        "lei-reeval", driver=MagicMock(), log=log,
        limit=2, api_key=None,
    )
    assert summary["enriched"] == 2
    # 2 per-LEI listing batches + 2 per-LEI retire batches
    assert log.batch.call_count == 4
    # Pin the interleaving order: L1 listing, L1 retire, L2 listing,
    # L2 retire — proves we're not just emitting all listings first
    # then all retires (which was the old at-end semantics).
    producers = [c.kwargs.get("producer") for c in log.batch.call_args_list]
    assert producers == ["load_openfigi"] * 4


def test_run_mode_isin_opens_one_batch_per_openfigi_batch(monkeypatch):
    """ISIN mode: per-batch commit boundary. With ``batch_size=10``
    (anonymous tier) and 15 input ISINs, we expect two OpenFIGI requests
    and therefore two ``log.batch(...)`` opens, NOT one batch over all
    15 results at the end. (The mock returns one canonical per batch
    so we can tell the batches apart.)"""
    rows = [
        {"isin": f"ISIN{i:03d}", "company_gmr_id": f"g{i}"}
        for i in range(15)
    ]
    monkeypatch.setitem(
        load_openfigi._MODES["isin"],
        "fetch", lambda _driver, _limit: rows,
    )

    def fake_query_openfigi(payload, _api_key):
        return [{"data": [{
            "ticker": entry["idValue"], "exchCode": "US",
            "marketSector": "Equity", "securityType2": "Common Stock", "micCode": "XNAS",
        }]} for entry in payload]

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    log, _emit = _mock_log()
    summary = load_openfigi._run_mode(
        "isin", driver=MagicMock(), log=log, limit=15, api_key=None,
    )
    assert summary["queried"] == 15
    # 15 IDs / batch_size=10 = 2 OpenFIGI requests → 2 commits
    assert log.batch.call_count == 2


# ── Bulk-hit happy path (formerly stubbed to empty by autouse fixture) ─


def test_run_mode_via_lei_uses_bulk_mapping_no_rest_call(monkeypatch):
    """Bulk-hit path: when ``bulk_isins`` has the LEI's entry, the
    OpenFIGI batch must be built from those ISINs WITHOUT touching the
    per-LEI REST endpoint. Pins the source-label too — a typo like
    'gleif-bulk' (dash vs underscore) would silently mis-count.
    """
    rows = [{"lei": "L1", "company_gmr_id": "g1", "witness_isins": []}]
    monkeypatch.setitem(
        load_openfigi._MODES["lei"],
        "fetch", lambda _driver, _limit: rows,
    )
    # Force-fail any REST call so the test would explode if the runner
    # ever bypassed the bulk dict.
    def _no_rest(_lei, client=None):
        raise AssertionError("must not call gleif_get_isins")
    monkeypatch.setattr(load_openfigi, "gleif_get_isins", _no_rest)

    captured = {}

    def fake_query_openfigi(payload, _api_key):
        captured["payload"] = payload
        return [{"data": [{
            "ticker": f"T-{entry['idValue']}", "exchCode": "LS",
            "marketSector": "Equity", "securityType2": "Common Stock", "micCode": "XLIS",
        }]} for entry in payload]

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    log, emit = _mock_log()
    summary = load_openfigi._run_mode_via_lei(
        "lei", driver=MagicMock(), log=log, limit=1, api_key=None,
        bulk_isins={"L1": ["I_FROM_BULK"]},
    )
    assert summary["enriched"] == 1
    assert summary["queried"] == 1
    assert captured["payload"] == [
        {"idType": "ID_ISIN", "idValue": "I_FROM_BULK"},
    ]
    # One emit per LEI's canonicals
    assert emit.upsert.call_count == 1


def test_resolve_lei_to_canonicals_bulk_source_label():
    """Direct unit test on the resolver: when ``bulk_isins`` is passed
    AND the row has no witness, the returned source label is the
    distinct 'gleif_bulk' value (not 'gleif'), so progress logs and
    the via_gleif counter can be aggregated correctly."""
    # The function calls query_openfigi which would hit the network;
    # short-circuit by passing a row with no resolvable ISINs. The
    # branch that picks the source label is independent of the
    # OpenFIGI call. Pass a non-empty bulk mapping for the LEI but
    # mock query_openfigi to return [] so the loop body is empty.
    with patch.object(
        load_openfigi, "query_openfigi", return_value=[],
    ):
        _canonicals, source, _unknown = load_openfigi._resolve_lei_to_canonicals(
            {"lei": "L1", "company_gmr_id": "g1", "witness_isins": []},
            batch_size=10, api_key=None,
            bulk_isins={"L1": ["I1"]},
        )
        assert source == "gleif_bulk"


def test_resolve_lei_to_canonicals_falls_back_to_rest_when_bulk_none(
    monkeypatch,
):
    """``bulk_isins=None`` keeps the legacy REST path for callers
    (diagnostic scripts, ad-hoc) that opt out of the bulk file."""
    rest_called = []
    def _rest(lei, _client=None):
        rest_called.append(lei)
        return ["I_FROM_REST"]
    monkeypatch.setattr(load_openfigi, "gleif_get_isins", _rest)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    with patch.object(
        load_openfigi, "query_openfigi", return_value=[],
    ):
        _canonicals, source, _unknown = load_openfigi._resolve_lei_to_canonicals(
            {"lei": "L1", "company_gmr_id": "g1", "witness_isins": []},
            batch_size=10, api_key=None,
            bulk_isins=None,
        )
        assert source == "gleif"
        assert rest_called == ["L1"]


def test_run_mode_via_lei_bulk_disabled_uses_rest(monkeypatch):
    """``bulk_isins_enabled=False`` short-circuits the inline build
    and falls through to the per-LEI REST path. Spies confirm the
    bulk loader was never called and the REST helper was."""
    rows = [{"lei": "L1", "company_gmr_id": "g1", "witness_isins": []}]
    monkeypatch.setitem(
        load_openfigi._MODES["lei"],
        "fetch", lambda _driver, _limit: rows,
    )
    bulk_spy = MagicMock(side_effect=AssertionError("must not build bulk"))
    monkeypatch.setattr(
        _gleif_isin_bulk, "load_isin_mapping", bulk_spy,
    )
    rest_calls = []
    def _rest(lei, _client=None):
        rest_calls.append(lei)
        return ["I1"]
    monkeypatch.setattr(load_openfigi, "gleif_get_isins", _rest)
    monkeypatch.setattr(
        load_openfigi, "query_openfigi",
        lambda payload, _api_key: [{"data": []} for _ in payload],
    )
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    log, _emit = _mock_log()
    load_openfigi._run_mode_via_lei(
        "lei", driver=MagicMock(), log=log, limit=1, api_key=None,
        bulk_isins_enabled=False,
    )
    bulk_spy.assert_not_called()
    assert rest_calls == ["L1"]


def test_load_openfigi_builds_bulk_once_for_mode_both(monkeypatch):
    """The hoist: ``mode=both`` triggers exactly ONE bulk-file stream
    across the lei + lei-reeval passes, NOT two."""
    # Both LEI modes return rows; need at least one row without witness
    # to trigger the bulk build.
    monkeypatch.setitem(
        load_openfigi._MODES["lei"],
        "fetch", lambda _driver, _limit: [
            {"lei": "L1", "company_gmr_id": "g1", "witness_isins": []},
        ],
    )
    monkeypatch.setitem(
        load_openfigi._MODES["lei-reeval"],
        "fetch", lambda _driver, _limit: [
            {"lei": "L2", "company_gmr_id": "g2",
             "witness_isins": [], "suspect_tickers": ["X.LS"]},
        ],
    )
    monkeypatch.setitem(
        load_openfigi._MODES["isin"],
        "fetch", lambda _driver, _limit: [],
    )
    bulk_calls = []
    def _bulk(target_leis=None, cache_dir=None, http_client=None):
        del cache_dir, http_client
        bulk_calls.append(set(target_leis or set()))
        return {}
    monkeypatch.setattr(
        _gleif_isin_bulk, "load_isin_mapping", _bulk,
    )
    monkeypatch.setattr(
        load_openfigi, "query_openfigi",
        lambda payload, _api_key: [{"data": []} for _ in payload],
    )
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    log, _emit = _mock_log()
    load_openfigi.load_openfigi(
        driver=MagicMock(), log=log, mode="both", limit=1, api_key=None,
    )
    assert len(bulk_calls) == 1, (
        f"expected exactly one bulk build, got {len(bulk_calls)}"
    )
    # And the target_leis set is the union of both lei modes' cohorts
    assert bulk_calls[0] == {"L1", "L2"}


# ── CLI flag: --bulk-isins / --no-bulk-isins + env var ────────────


def _stub_main_deps(monkeypatch):
    """Stub Neo4j driver + EventLog so main() runs without I/O."""
    monkeypatch.setattr(
        load_openfigi.GraphDatabase, "driver",
        lambda *a, **kw: MagicMock(),
    )
    monkeypatch.setattr(
        load_openfigi.EventLog, "from_env",
        classmethod(lambda cls: MagicMock()),
    )


def _capture_load_openfigi(monkeypatch):
    """Capture the kwargs main() passes through. Returns a dict that
    accumulates the call's kwargs."""
    captured = {}
    def _capture(driver, log, **kwargs):
        del driver, log
        captured.update(kwargs)
        return {}
    monkeypatch.setattr(load_openfigi, "load_openfigi", _capture)
    return captured


def test_cli_default_is_bulk_enabled(monkeypatch):
    """No flag, no env var → bulk-isins on (the production default)."""
    monkeypatch.delenv("OPENFIGI_NO_BULK_ISINS", raising=False)
    _stub_main_deps(monkeypatch)
    captured = _capture_load_openfigi(monkeypatch)
    load_openfigi.main([])
    assert captured["bulk_isins_enabled"] is True


def test_cli_no_bulk_isins_flag_disables(monkeypatch):
    """``--no-bulk-isins`` (paired half of BooleanOptionalAction)
    flips the default off."""
    monkeypatch.delenv("OPENFIGI_NO_BULK_ISINS", raising=False)
    _stub_main_deps(monkeypatch)
    captured = _capture_load_openfigi(monkeypatch)
    load_openfigi.main(["--no-bulk-isins"])
    assert captured["bulk_isins_enabled"] is False


def test_cli_env_var_disables(monkeypatch):
    """OPENFIGI_NO_BULK_ISINS=1 disables the bulk path without any
    CLI flag — the escape hatch for ops to flip behaviour via env."""
    monkeypatch.setenv("OPENFIGI_NO_BULK_ISINS", "1")
    _stub_main_deps(monkeypatch)
    captured = _capture_load_openfigi(monkeypatch)
    load_openfigi.main([])
    assert captured["bulk_isins_enabled"] is False


def test_cli_bulk_isins_flag_overrides_disabling_env(monkeypatch):
    """The whole point of the BooleanOptionalAction refactor: an
    operator can re-enable bulk from the CLI even when the env var
    says off. Without the paired flag this scenario was unreachable."""
    monkeypatch.setenv("OPENFIGI_NO_BULK_ISINS", "true")
    _stub_main_deps(monkeypatch)
    captured = _capture_load_openfigi(monkeypatch)
    load_openfigi.main(["--bulk-isins"])
    assert captured["bulk_isins_enabled"] is True


def test_run_mode_via_lei_skips_sleep_when_no_openfigi_call(monkeypatch):
    """Pacing-sleep is OpenFIGI's rate-limit budget. With the bulk
    file most LEIs have no ISINs and we never call OpenFIGI for them
    — so we must not burn the per-LEI sleep either. Previous shape
    paid ~3 s × 8 858 non-issuer LEIs = 7.4 h of pure idle on a
    10 k-LEI bulk run. This test pins the gate.
    """
    rows = [
        {"lei": "L_HAS_ISIN", "company_gmr_id": "g1",
         "witness_isins": ["W1"]},
        {"lei": "L_NO_ISIN", "company_gmr_id": "g2",
         "witness_isins": []},
        {"lei": "L_NO_ISIN_2", "company_gmr_id": "g3",
         "witness_isins": []},
    ]
    monkeypatch.setitem(
        load_openfigi._MODES["lei"],
        "fetch", lambda _driver, _limit: rows,
    )
    monkeypatch.setattr(
        load_openfigi, "query_openfigi",
        lambda payload, _api_key: [{"data": []} for _ in payload],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        load_openfigi.time, "sleep", sleeps.append,
    )
    log, _emit = _mock_log()
    # Bulk dict has the witness-having row's LEI only — irrelevant,
    # witness path takes precedence anyway. The two "no_isin" rows
    # land in source="none" and must NOT sleep.
    load_openfigi._run_mode_via_lei(
        "lei", driver=MagicMock(), log=log, limit=3, api_key=None,
        bulk_isins={},
    )
    # Exactly one sleep, for the witness-having LEI.
    assert sleeps == [3.0], (
        f"expected one sleep for the OpenFIGI-calling LEI, got {sleeps}"
    )


def test_resolve_lei_to_canonicals_paces_between_batches(monkeypatch):
    """A multi-ISIN issuer must pace each OpenFIGI request inside the
    LEI, not just between LEIs in the outer loop. Mass-issuer LEIs
    (Mota-Engil: 48 ISINs → 5 anonymous-tier batches) without this
    inner sleep fire 5 POSTs in <1 s and trip the 25 req/min limit.
    The pacing is asymmetric: no sleep before the first batch (the
    outer loop's inter-LEI sleep already covered that gap), then
    one sleep_s wait before each subsequent batch.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(load_openfigi.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        load_openfigi, "query_openfigi",
        lambda payload, _api_key: [
            {"data": [{"ticker": "T", "exchCode": "X",
                       "marketSector": "Equity", "securityType2": "Common Stock"}]}
            for _ in payload
        ],
    )
    # 25 ISINs / batch_size=10 → 3 batches → 2 inter-batch sleeps
    bulk = {"MASS_ISSUER": [f"ISIN{i}" for i in range(25)]}
    _canonicals, source, _unknown = load_openfigi._resolve_lei_to_canonicals(
        {"lei": "MASS_ISSUER", "company_gmr_id": "g1",
         "witness_isins": []},
        batch_size=10, api_key=None, bulk_isins=bulk,
        sleep_between_batches=3.0,
    )
    assert source == "gleif_bulk"
    # Exactly 2 inter-batch sleeps for the 3 batches; no leading sleep
    # before the first batch.
    assert sleeps == [3.0, 3.0]


def test_resolve_lei_to_canonicals_no_inter_batch_sleep_when_disabled(
    monkeypatch,
):
    """Default sleep_between_batches=0 keeps the existing test fixtures
    sleep-free for callers that don't opt in (mocks, direct invocations).
    """
    sleeps: list[float] = []
    monkeypatch.setattr(load_openfigi.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        load_openfigi, "query_openfigi",
        lambda payload, _api_key: [
            {"data": [{"ticker": "T", "exchCode": "X",
                       "marketSector": "Equity", "securityType2": "Common Stock"}]}
            for _ in payload
        ],
    )
    bulk = {"L1": [f"ISIN{i}" for i in range(25)]}
    load_openfigi._resolve_lei_to_canonicals(
        {"lei": "L1", "company_gmr_id": "g1", "witness_isins": []},
        batch_size=10, api_key=None, bulk_isins=bulk,
        # sleep_between_batches default = 0
    )
    assert not sleeps


# ── Concurrency: rate limiter + parallel LEI resolution ───────────


def test_rate_limiter_spaces_calls_by_interval(monkeypatch):
    """_RateLimiter reserves evenly-spaced slots: after the first
    (free) grant, every subsequent wait() sleeps exactly one interval
    (60/rate_per_min) so the aggregate rate can't exceed the ceiling."""
    clock = {"t": 0.0}
    slept: list[float] = []
    monkeypatch.setattr(load_openfigi.time, "monotonic", lambda: clock["t"])

    def fake_sleep(d):
        slept.append(d)
        clock["t"] += d  # sleeping advances the (fake) clock

    monkeypatch.setattr(load_openfigi.time, "sleep", fake_sleep)

    limiter = load_openfigi._RateLimiter(rate_per_min=600)  # 0.1s interval
    for _ in range(5):
        limiter.wait()

    # First grant is immediate (no sleep); the next four each wait one
    # 0.1s interval.
    assert slept == pytest.approx([0.1, 0.1, 0.1, 0.1])


def _lei_rows(n, with_isin):
    return [
        {"lei": f"L{i}", "company_gmr_id": f"g{i}",
         "witness_isins": ([f"I{i}"] if with_isin(i) else []),
         "suspect_tickers": []}
        for i in range(n)
    ]


def test_run_mode_via_lei_concurrent_emits_every_canonical(monkeypatch):
    """With concurrency>1 and an API key, resolution runs in a thread
    pool but every LEI's canonical is still emitted exactly once (on the
    main thread), so no enrichment is dropped or double-counted."""
    rows = _lei_rows(12, lambda i: True)
    monkeypatch.setitem(load_openfigi._MODES["lei"], "fetch",
                        lambda _d, _l: rows)

    def fake_query_openfigi(payload, _api_key):
        isin = payload[0]["idValue"]
        return [{"data": [{"ticker": f"T{isin}", "exchCode": "XX",
                           "marketSector": "Equity", "securityType2": "Common Stock"}]}]

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)

    log, emit = _mock_log()
    summary = load_openfigi._run_mode_via_lei(
        "lei", driver=MagicMock(), log=log, limit=12,
        api_key="KEY", concurrency=4,
    )
    assert summary["queried"] == 12
    assert summary["enriched"] == 12
    assert emit.upsert.call_count == 12


def test_run_mode_via_lei_concurrent_matches_serial(monkeypatch):
    """The parallel path is behaviour-preserving: same summary counts
    and same multiset of emitted envelopes as the serial path, for an
    identical mixed cohort (some LEIs resolve, some don't)."""
    rows = _lei_rows(20, lambda i: i % 3 == 0)  # ~1/3 have a witness ISIN

    def fake_query_openfigi(payload, _api_key):
        isin = payload[0]["idValue"]
        return [{"data": [{"ticker": f"T{isin}", "exchCode": "XX",
                           "marketSector": "Equity", "securityType2": "Common Stock"}]}]

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)

    def run(concurrency):
        monkeypatch.setitem(load_openfigi._MODES["lei"], "fetch",
                            lambda _d, _l: rows)
        log, emit = _mock_log()
        summary = load_openfigi._run_mode_via_lei(
            "lei", driver=MagicMock(), log=log, limit=20,
            api_key="KEY", concurrency=concurrency,
        )
        types = sorted(c.args[0] for c in emit.upsert.call_args_list)
        return summary, types

    serial_summary, serial_types = run(1)
    conc_summary, conc_types = run(8)

    assert conc_summary == serial_summary
    assert conc_types == serial_types
    assert serial_summary["enriched"] == 7  # ceil(20/3) LEIs with an ISIN


# ── securityType2 classification (company vs fund) ────────────────


def test_equity_canonicals_tags_company_and_fund_classes():
    """securityType2 drives entity_class: Common Stock → company,
    Mutual Fund (open/closed-end funds, ETPs, fund-of-funds) → fund.
    Unknown types are skipped AND counted — never silently kept."""
    response = [
        {"data": [
            {"ticker": "ACME", "exchCode": "LN",
             "marketSector": "Equity",
             "securityType2": "Common Stock",
             "securityType": "Common Stock"},
            {"ticker": "ACMEFND", "exchCode": "LN",
             "marketSector": "Equity",
             "securityType2": "Mutual Fund",
             "securityType": "Open-End Fund"},
            {"ticker": "WEIRD", "exchCode": "LN",
             "marketSector": "Equity",
             "securityType2": "Equity WRT",
             "securityType": "Equity WRT"},
        ]},
    ]
    unknown: dict = {}
    out = load_openfigi._equity_canonicals_from_response(
        response, ["ISIN1"], lei="LEI", company_gmr_id="gid",
        unknown_types=unknown,
    )
    assert [(r["ticker"], r["entity_class"]) for r in out] == [
        ("ACME", "company"), ("ACMEFND", "fund"),
    ]
    assert out[1]["security_type"] == "Open-End Fund"
    assert unknown == {"Equity WRT": 1}


def test_run_mode_via_lei_does_not_emit_fund_class_listings(monkeypatch):
    """Fund-class instruments are counted in the summary but NOT
    emitted as Company listings — that cohort belongs to the
    :InvestmentFund model (UpsertInvestmentFund, Track B)."""
    rows = [
        {"lei": "L1", "company_gmr_id": "g1", "witness_isins": ["I1"]},
        {"lei": "L2", "company_gmr_id": "g2", "witness_isins": ["I2"]},
    ]
    monkeypatch.setitem(load_openfigi._MODES["lei"], "fetch",
                        lambda _d, _l: rows)

    def fake_query_openfigi(payload, _api_key):
        isin = payload[0]["idValue"]
        sec2 = "Common Stock" if isin == "I1" else "Mutual Fund"
        return [{"data": [{"ticker": f"T{isin}", "exchCode": "XX",
                           "marketSector": "Equity",
                           "securityType2": sec2}]}]

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)
    log, emit = _mock_log()
    summary = load_openfigi._run_mode_via_lei(
        "lei", driver=MagicMock(), log=log, limit=2, api_key=None,
    )
    assert summary["enriched"] == 1      # only the Common Stock
    assert summary["funds"] == 1
    assert emit.upsert.call_count == 1
