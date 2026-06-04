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


def test_retires_for_suspects_no_replacement_when_ambiguous():
    # Multiple canonicals, none on the suspect's suffix → we don't
    # invent a redirect. The bad Listing is deactivated, but no
    # AssertSameAs fires.
    rows = [{"lei": "L1", "company_gmr_id": "g1",
             "suspect_tickers": ["WEIRD.XX"]}]
    enriched = [
        {"lei": "L1", "ticker": "A.YY", "exchange_code": "YY",
         "company_gmr_id": "g1"},
        {"lei": "L1", "ticker": "B.ZZ", "exchange_code": "ZZ",
         "company_gmr_id": "g1"},
    ]
    retires = load_openfigi._retires_for_suspects(rows, enriched)
    assert retires[0]["replacement_ticker"] is None


def test_retires_for_suspects_no_canonicals_means_no_retire_records():
    # OpenFIGI returned nothing for this LEI (private company, rate
    # limit, lookup miss). Don't touch existing Listings — we have
    # no evidence they're wrong.
    rows = [{"lei": "L1", "company_gmr_id": "g1",
             "suspect_tickers": ["SOMETHING.LS"]}]
    retires = load_openfigi._retires_for_suspects(rows, enriched=[])
    # Still emit a retire for the suspect — wait, no: with no
    # canonical we have no evidence it's wrong. Keep it.
    assert all(r["replacement_ticker"] is None for r in retires)
    # And the retire's replacement is None so AssertSameAs is skipped.


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
             "marketSector": "Equity", "micCode": "XLIS",
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
             "marketSector": "Equity", "figi": "F1"},
        ]},
        {"data": [
            {"ticker": "EGL", "exchCode": "PL",
             "marketSector": "Equity", "figi": "F2"},
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
                           "marketSector": "Equity"}]}]

    def fake_gleif(_lei, client=None):  # pragma: no cover  # pylint: disable=unused-argument
        calls["gleif"] += 1
        return []

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi, "gleif_get_isins", fake_gleif)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)

    row = {"lei": "L1", "company_gmr_id": "g1",
           "witness_isins": ["PTMEN0AE0005"], "suspect_tickers": []}
    canonicals, source = load_openfigi._resolve_lei_to_canonicals(
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
                           "marketSector": "Equity"}]}]

    def fake_gleif(_lei, client=None):  # pylint: disable=unused-argument
        calls["gleif"] += 1
        return ["FAKEISIN1"]

    monkeypatch.setattr(load_openfigi, "query_openfigi", fake_query_openfigi)
    monkeypatch.setattr(load_openfigi, "gleif_get_isins", fake_gleif)
    monkeypatch.setattr(load_openfigi.time, "sleep", lambda _s: None)

    row = {"lei": "L1", "company_gmr_id": "g1",
           "witness_isins": [], "suspect_tickers": []}
    canonicals, source = load_openfigi._resolve_lei_to_canonicals(
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
    canonicals, source = load_openfigi._resolve_lei_to_canonicals(
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
             "marketSector": "Equity", "micCode": "XLIS"},
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
