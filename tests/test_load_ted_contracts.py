"""Tests for the TED contract loader (post-event-log)."""
from unittest.mock import MagicMock, patch

import pytest

from src.etl import load_ted_contracts
from src.etl.load_ted_contracts import load_contracts


@pytest.fixture(autouse=True)
def _stub_ted_lookup(monkeypatch):
    """The ETL now resolves the publication-number via TED's v3 search
    on every iteration; pin the helper to a stable value so tests
    don't hit the network. The default — "295342-2026" — mirrors the
    real shape; individual tests can re-patch for None or exception
    paths."""
    monkeypatch.setattr(
        "src.etl.load_ted_contracts._resolve_pub_num_or_none",
        lambda _uuid: "295342-2026",
    )


@pytest.fixture(autouse=True)
def _stub_already_loaded(monkeypatch):
    """Idempotency gate: default to "not yet loaded" so existing happy-
    path tests exercise the emit pipeline. Tests that want to assert the
    skip path re-patch this to return True."""
    monkeypatch.setattr(
        "src.etl.load_ted_contracts._already_loaded",
        lambda _session, _nid: False,
    )


def _mock_driver_and_session():
    """Create a mock Neo4j driver with session (TedMatcher reads
    Neo4j to resolve gmr_ids; the rest of the writes go to events)."""
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def _mock_matcher(stub_authority_id: str, stub_company_gmr: str):
    """Build a TedMatcher stand-in. ``match_authority`` returns a
    fixed authority_id; ``match_company`` returns an object with the
    fixed gmr_id."""
    matcher = MagicMock()
    matcher.match_authority.return_value = stub_authority_id
    company_match = MagicMock()
    company_match.gmr_id = stub_company_gmr
    matcher.match_company.return_value = company_match
    matcher.stats.summary.return_value = {
        "total": 0, "by_layer": {}, "vies_failures": 0,
    }
    return matcher


def _stub_award(currency="EUR", value=1000.0, contractor_org_id="O1"):
    award = MagicMock()
    award.contractor_org_id = contractor_org_id
    award.value = value
    award.currency = currency
    award.award_date = "2025-09-15"
    award.conclusion_date = None
    award.tenders_received = 1  # single-bidder, for the integrity assertions
    return award


def _stub_notice(*, awards, organizations):
    notice = MagicMock()
    # Match real eForms parsing: publication_number is never populated
    # by the parser (TED assigns it post-ingest), only notice_id is.
    # The ETL now uses notice_id directly as ted_notice_id and
    # resolves publication-number out-of-band via TED's v3 search API.
    notice.publication_number = None
    notice.notice_id = "912f1717-1ace-413d-aa61-cd21cd6b95e7"
    notice.title = "Some contract"
    notice.description = "Procurement of stuff"
    notice.issue_date = "2025-09-01"
    notice.dispatch_date = "2025-09-01"
    notice.awards = awards
    notice.organizations = organizations
    notice.cpv_main = "45000000"
    notice.procedure_type = "open"
    notice.award_criterion_type = "price"
    notice.submission_deadline = "2025-08-15"
    notice.is_framework = False
    notice.eu_funded = True
    notice.funding_programme = "RRF"
    notice.notice_type = "can-standard"
    notice.currency = "EUR"
    notice.total_value = None
    notice.place_nuts = "FR101"
    notice.language = "fr"
    buyer = MagicMock()
    buyer.name = "Conseil constitutionnel"
    buyer.country = "FR"
    buyer.legal_id = MagicMock(value="FR-CC-001", scheme_name="NATIONAL")
    notice.buyer.return_value = buyer
    return notice


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_emits_authority_company_and_contract(
    mock_matcher_cls, mock_stream,
):
    """A single award notice produces UpsertAuthority + UpsertCompany +
    UpsertContract (one each, in that order, deduped per run)."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="11111111-2222-5333-8444-555555555555",
        stub_company_gmr="00040372-dad6-5d34-882c-8b8624b4e734",
    )
    contractor = MagicMock()
    contractor.name = "Adyen N.V."
    contractor.country = "NL"
    contractor.legal_id = MagicMock(value="NL850456592B01", scheme_name="VAT")
    notice = _stub_notice(
        awards=[_stub_award()],
        organizations={"O1": contractor},
    )
    mock_stream.return_value = iter([notice])

    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    res = load_contracts(driver, log, "/fake/path.tar.gz")
    assert res["total"] == 1
    assert res["skipped"] == 0

    types = [c.args[0] for c in emit.upsert.call_args_list]
    assert types == ["UpsertAuthority", "UpsertCompany", "UpsertContract"]
    # The skipped counter is the path the idempotent-skip operator
    # exercises; per-notice transactions mean the emit-side counts
    # are the source of truth for "how many notices were processed",
    # not the return shape, which only carries totals/skips/elapsed.


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_authority_dedup_within_one_run(
    mock_matcher_cls, mock_stream,
):
    """Two notices with the same buyer should produce one
    UpsertAuthority event, not two."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="aaaa-1111",
        stub_company_gmr="bbbb-2222",
    )
    contractor = MagicMock()
    contractor.name = "Vendor"
    contractor.country = "FR"
    contractor.legal_id = None
    notices = [
        _stub_notice(awards=[_stub_award()], organizations={"O1": contractor}),
        _stub_notice(awards=[_stub_award()], organizations={"O1": contractor}),
    ]
    mock_stream.return_value = iter(notices)

    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    load_contracts(driver, log, "/fake/path.tar.gz")

    auth_emits = [
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertAuthority"
    ]
    assert len(auth_emits) == 1
    contract_emits = [
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ]
    assert len(contract_emits) == 2


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_contract_payload_carries_authority_and_company_links(
    mock_matcher_cls, mock_stream,
):
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1",
        stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Adyen N.V."
    contractor.country = "NL"
    contractor.legal_id = None
    mock_stream.return_value = iter([
        _stub_notice(
            awards=[_stub_award()],
            organizations={"O1": contractor},
        ),
    ])

    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    load_contracts(driver, log, "/fake/path.tar.gz")

    contract_call = next(
        c for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract"
    )
    payload = contract_call.kwargs["payload"]
    assert payload["authority_id"] == "auth-1"
    # Tender-integrity fields threaded through from the parsed notice/award.
    assert payload["procedure_type"] == "open"
    assert payload["tenders_received"] == 1
    assert payload["award_criterion_type"] == "price"
    assert payload["submission_deadline"] == "2025-08-15"
    assert payload["is_framework"] is False
    assert payload["eu_funded"] is True
    assert payload["funding_programme"] == "RRF"
    assert payload["company_gmr_id"] == "company-1"
    assert payload["ted_notice_id"] == "912f1717-1ace-413d-aa61-cd21cd6b95e7"
    assert payload["ted_publication_number"] == "295342-2026"
    assert payload["cpv"] == "45000000"
    # The acquirer (buyer.country = "FR") cascades onto the Contract
    # as alpha-3 "FRA". Before this fix, Contract had no country at
    # all — the dashboard's "contracts by country" panel was empty
    # for 56k staging contracts.
    assert payload["country"] == "FRA"


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_contract_iri_keyed_on_uuid_not_publication_number(
    mock_matcher_cls, mock_stream,
):
    """The Contract IRI is built from ted_notice_id (the stable UUID),
    NOT from the publication-number. Why: TED publishes the pub-num
    after the eForms XML appears in the daily archive, and may revise
    it on re-publication. Keying RDF identifiers on a value that can
    change after first ingest breaks downstream consumers that have
    already cached the IRI."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1",
        stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Adyen N.V."
    contractor.country = "NL"
    contractor.legal_id = None
    mock_stream.return_value = iter([
        _stub_notice(
            awards=[_stub_award()],
            organizations={"O1": contractor},
        ),
    ])

    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    load_contracts(driver, log, "/fake/path.tar.gz")

    contract_call = next(
        c for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract"
    )
    iri = contract_call.kwargs["iri"]
    assert iri.endswith("/912f1717-1ace-413d-aa61-cd21cd6b95e7"), iri
    # The pub-num value is still on the payload — just not in the IRI.
    assert contract_call.kwargs["payload"]["ted_publication_number"] == \
        "295342-2026"


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_contract_publication_number_null_when_lookup_returns_none(
    mock_matcher_cls, mock_stream, monkeypatch,
):
    """Notices whose TED publication-number can't be resolved at ETL
    time — TED hasn't assigned one yet, or the search API returned
    no match — emit the Contract with ``ted_publication_number=None``.
    The builder strips None values, so the field is just absent from
    the payload (not the empty string). The runtime /ted-link
    redirector then falls back to its own live lookup on click."""
    monkeypatch.setattr(
        "src.etl.load_ted_contracts._resolve_pub_num_or_none",
        lambda _uuid: None,
    )
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1",
        stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Adyen N.V."
    contractor.country = "NL"
    contractor.legal_id = None
    mock_stream.return_value = iter([
        _stub_notice(
            awards=[_stub_award()],
            organizations={"O1": contractor},
        ),
    ])

    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    load_contracts(driver, log, "/fake/path.tar.gz")

    contract_call = next(
        c for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract"
    )
    payload = contract_call.kwargs["payload"]
    assert "ted_publication_number" not in payload, (
        "builder must elide None — leaving the field absent so the "
        f"sink doesn't write a null property; got payload keys "
        f"{list(payload)}"
    )


def test_resolve_pub_num_or_none_swallows_transport_errors(
    monkeypatch,
):
    """ETL must never fail a contract row because TED's search API
    is unreachable — the deeper fontem-events transaction would
    rollback the whole batch. Wrapper returns None on httpx errors
    so the row persists with no pub-num and the runtime redirector
    picks up the slack."""
    import httpx  # pylint: disable=import-outside-toplevel
    import importlib  # pylint: disable=import-outside-toplevel
    from src.services import ted_lookup  # pylint: disable=import-outside-toplevel
    ted_lookup.resolve_publication_number.cache_clear()

    # The autouse fixture stubs _resolve_pub_num_or_none itself —
    # undo it on this module attribute so we exercise the real
    # wrapper below.
    monkeypatch.undo()
    importlib.reload(load_ted_contracts)

    def _explode(_uuid):
        raise httpx.ConnectError("TED is down")

    monkeypatch.setattr(
        "src.etl.load_ted_contracts.resolve_publication_number",
        _explode,
    )
    out = load_ted_contracts._resolve_pub_num_or_none(  # pylint: disable=protected-access
        "912f1717-1ace-413d-aa61-cd21cd6b95e7",
    )
    assert out is None


def test_resolve_pub_num_or_none_returns_none_on_no_match(monkeypatch):
    """TedLookupError (TED has no record of the UUID) → None, same as
    the transport-error path. The row persists; the redirector
    surfaces the 404 from its own lookup on click."""
    import importlib  # pylint: disable=import-outside-toplevel
    from src.services import ted_lookup  # pylint: disable=import-outside-toplevel
    from src.services.ted_lookup import TedLookupError  # pylint: disable=import-outside-toplevel
    ted_lookup.resolve_publication_number.cache_clear()

    monkeypatch.undo()
    importlib.reload(load_ted_contracts)

    def _no_match(_uuid):
        raise TedLookupError("TED has no published notice for X")

    monkeypatch.setattr(
        "src.etl.load_ted_contracts.resolve_publication_number",
        _no_match,
    )
    out = load_ted_contracts._resolve_pub_num_or_none(  # pylint: disable=protected-access
        "912f1717-1ace-413d-aa61-cd21cd6b95e7",
    )
    assert out is None


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_skips_award_with_unknown_contractor(
    mock_matcher_cls, mock_stream,
):
    """Awards whose contractor org isn't in notice.organizations are
    skipped entirely (no Contract or Company emitted)."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1",
        stub_company_gmr="company-1",
    )
    notice = _stub_notice(
        awards=[_stub_award(contractor_org_id="MISSING")],
        organizations={"O1": MagicMock()},  # MISSING isn't here
    )
    mock_stream.return_value = iter([notice])

    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    res = load_contracts(driver, log, "/fake/path.tar.gz")
    # Per-notice transaction commits the (empty awards) notice — the
    # Authority emit still goes out because we resolve buyer before
    # iterating awards, but no Contract is emitted.
    assert res["total"] == 1
    auth_emits = [
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertAuthority"
    ]
    assert len(auth_emits) == 1
    contract_emits = [
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ]
    assert contract_emits == []


# ── idempotency: skip notices already in Neo4j ──────────────────────


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_skips_notice_already_in_neo4j(
    mock_matcher_cls, mock_stream, monkeypatch,
):
    """Idempotent re-run: notices whose ``ted_notice_id`` already
    exists on a ``Contract`` node in Neo4j are skipped entirely — no
    TED-search call, no eForms work, no emit. The whole point is
    that re-running the same month is O(1)-per-notice instead of
    paying the full per-notice cost again."""
    monkeypatch.setattr(
        "src.etl.load_ted_contracts._already_loaded",
        lambda _session, _nid: True,
    )
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1",
        stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Adyen N.V."
    contractor.country = "NL"
    contractor.legal_id = None
    mock_stream.return_value = iter([
        _stub_notice(
            awards=[_stub_award()],
            organizations={"O1": contractor},
        ),
    ])

    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    res = load_contracts(driver, log, "/fake/path.tar.gz")

    assert res["total"] == 0
    assert res["skipped"] == 1
    # No emit calls of any kind — the skip is total.
    assert emit.upsert.call_args_list == []
    # And no batch was opened — per-notice transactions are not
    # started for skipped notices, which is the whole win.
    assert log.batch.call_args_list == []


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_rescore_reingests_already_loaded_notice(
    mock_matcher_cls, mock_stream, monkeypatch,
):
    """With rescore=True the already-loaded skip is bypassed so the
    notice is re-parsed and re-emitted (the backfill path). The sink
    MERGEs, so values overwrite in place."""
    monkeypatch.setattr(
        "src.etl.load_ted_contracts._already_loaded",
        lambda _session, _nid: True,  # pretend it is already in Neo4j
    )
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1", stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Adyen N.V."
    contractor.country = "NL"
    contractor.legal_id = None
    mock_stream.return_value = iter([
        _stub_notice(awards=[_stub_award()], organizations={"O1": contractor}),
    ])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    res = load_contracts(driver, log, "/fake/path.tar.gz", rescore=True)

    # Not skipped — the notice was re-processed and emitted.
    assert res["total"] == 1
    assert res["skipped"] == 0
    assert any(c.args[0] == "UpsertContract" for c in emit.upsert.call_args_list)


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_per_notice_transactions_one_batch_per_notice(
    mock_matcher_cls, mock_stream,
):
    """Per-notice transactions: two notices → two ``log.batch(...)``
    contexts. The old whole-archive batch kept hours of work in one
    open Postgres transaction; this pins the new commit boundary so
    a regression to "one batch per archive" trips the suite."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1",
        stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Vendor"
    contractor.country = "FR"
    contractor.legal_id = None
    notices = [
        _stub_notice(awards=[_stub_award()], organizations={"O1": contractor}),
        _stub_notice(awards=[_stub_award()], organizations={"O1": contractor}),
    ]
    # Distinct notice_ids so the idempotency stub treats them as
    # separate notices.
    notices[0].notice_id = "11111111-1111-1111-1111-111111111111"
    notices[1].notice_id = "22222222-2222-2222-2222-222222222222"
    mock_stream.return_value = iter(notices)

    driver, _session = _mock_driver_and_session()
    log, _emit = _mock_log()
    load_contracts(driver, log, "/fake/path.tar.gz")

    assert len(log.batch.call_args_list) == 2


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_skip_pub_num_lookup_skips_ted_v3_search(
    mock_matcher_cls, mock_stream, monkeypatch,
):
    """``skip_pub_num_lookup=True`` short-circuits the per-notice TED
    v3 search and emits Contracts with no ``ted_publication_number``.
    The builder strips None so the property is absent — the
    backfill job fills it in later. The bulk historical loader
    flips this on to avoid paying ~500ms × millions of notices to
    TED's API for a value that backfill can do in parallel."""
    called: list[str] = []

    def _should_not_be_called(_uuid):
        called.append(_uuid)
        return "should-not-appear"

    monkeypatch.setattr(
        "src.etl.load_ted_contracts._resolve_pub_num_or_none",
        _should_not_be_called,
    )

    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1",
        stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Adyen N.V."
    contractor.country = "NL"
    contractor.legal_id = None
    mock_stream.return_value = iter([
        _stub_notice(
            awards=[_stub_award()],
            organizations={"O1": contractor},
        ),
    ])

    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    load_contracts(
        driver, log, "/fake/path.tar.gz",
        skip_pub_num_lookup=True,
    )

    assert not called, (
        "skip_pub_num_lookup=True must not call _resolve_pub_num_or_none"
    )
    contract_call = next(
        c for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract"
    )
    payload = contract_call.kwargs["payload"]
    assert "ted_publication_number" not in payload


# ── --year/--month default to current calendar month ────────────────


def test_main_no_args_runs_incremental_from_watermark(monkeypatch):
    """No-args (the daily cron shape) now runs the incremental search-API
    path from the watermark forward — NOT the old current-month monthly
    download. TED doesn't publish a month's package until the month ends,
    so that default 404-ed every single day."""
    from datetime import date, timedelta  # pylint: disable=import-outside-toplevel
    captured: dict = {}

    def _fake_incremental(driver, log, since, until, **kw):  # pylint: disable=unused-argument
        captured["since"] = since
        captured["until"] = until

    def _must_not_download(*a, **kw):
        raise AssertionError("monthly download must not run for the no-args default")

    monkeypatch.setattr(load_ted_contracts, "load_contracts_incremental", _fake_incremental)
    monkeypatch.setattr(load_ted_contracts, "_download_monthly", _must_not_download)
    monkeypatch.setattr(
        load_ted_contracts, "_read_watermark",
        lambda session, wmid=None: "2026-06-20",
    )
    monkeypatch.setattr("src.etl.load_cpv.load_cpv", lambda *a, **kw: None)
    monkeypatch.setattr(load_ted_contracts.GraphDatabase, "driver",
                        lambda *a, **kw: MagicMock())
    monkeypatch.setattr(load_ted_contracts.EventLog, "from_env",
                        classmethod(lambda cls: MagicMock()))

    load_ted_contracts.main([])

    assert captured["since"] == date(2026, 6, 20) + timedelta(days=1)
    assert captured["until"] == date.today()


def test_main_overrides_year_month_when_explicit(monkeypatch):
    """When --year/--month are passed explicitly, they take precedence
    over the current-date default. (Lets backfill jobs pin an older
    month: `python -m src.etl.load_ted_contracts --year 2026 --month 4`.)
    """
    captured: dict = {}

    def _fake_download(year, month, dest):
        captured["year"] = year
        captured["month"] = month
        captured["dest"] = dest
        raise SystemExit(0)

    monkeypatch.setattr(load_ted_contracts, "_download_monthly", _fake_download)
    monkeypatch.setattr(load_ted_contracts.GraphDatabase, "driver",
                        lambda *a, **kw: MagicMock())
    monkeypatch.setattr(load_ted_contracts.EventLog, "from_env",
                        classmethod(lambda cls: MagicMock()))

    try:
        load_ted_contracts.main(["--year", "2024", "--month", "6"])
    except SystemExit:
        pass

    assert captured["year"] == 2024
    assert captured["month"] == 6


# ── Value-sanity cap (extra-zero authority eForms data entry errors) ─


def _stub_currency_svc(value_eur: float | None, parsed_value: float = None):
    """A currency-service mock that returns the requested EUR amount
    so we can exercise the sanity-cap branch deterministically.
    Resolution + conversion happen inside the loader; here we short
    circuit both."""
    from decimal import Decimal  # pylint: disable=import-outside-toplevel
    svc = MagicMock()
    svc.parse_value.return_value = (
        Decimal(str(parsed_value if parsed_value is not None
                    else value_eur or 0)),
        False,
    )
    svc.resolve_currency.return_value = ("EUR", False)
    svc.to_eur.return_value = (
        Decimal(str(value_eur)) if value_eur is not None else None
    )
    return svc


def _lot_with_estimate(lot_id: str, estimated_value: float | None,
                       currency: str = "EUR"):
    """A Lot stub mirroring the eForms-parser ``Lot`` dataclass."""
    lot = MagicMock()
    lot.lot_id = lot_id
    lot.estimated_value = estimated_value
    lot.currency = currency
    return lot


def _fx_svc(rate: float = 1.0):
    """A currency-service mock that converts PROPORTIONALLY:
    ``parse_value(x) == x`` and ``to_eur(x) == x * rate``. Unlike
    ``_stub_currency_svc`` (which returns one fixed EUR amount for any
    input), this lets the estimate, total, and payable each convert to a
    distinct EUR value — required to exercise the confidence scorer,
    which cross-checks those signals against each other."""
    from decimal import Decimal  # pylint: disable=import-outside-toplevel
    svc = MagicMock()
    svc.parse_value.side_effect = lambda v: (
        (Decimal(str(v)), False) if v is not None else (None, False)
    )
    svc.resolve_currency.return_value = ("EUR", False)
    svc.to_eur.side_effect = lambda parsed, ccy, date: (
        Decimal(str(parsed)) * Decimal(str(rate)) if parsed is not None else None
    )
    return svc


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_value_dropped_when_award_exceeds_estimate_by_huge_ratio(
    mock_matcher_cls, mock_stream,
):
    """The canonical Swedish bus fixture: payable
    2_110_249_000_000_000 SEK (~€182 T) with an
    ``EstimatedOverallContractAmount`` of 2_000_000_000 SEK on the same
    lot — ratio ~1,055,124x. New behaviour: the value is STORED (we never
    destroy data) but the contract is flagged low-confidence so it is
    excluded from default aggregates. The estimate and payable are kept
    alongside for review."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1", stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Nobina Sverige AB"
    contractor.country = "SWE"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award(currency="SEK", value=2_110_249_000_000_000.0)],
        organizations={"O1": contractor},
    )
    notice.lots = [_lot_with_estimate("LOT-0000", 2_000_000_000.0,
                                       currency="SEK")]
    mock_stream.return_value = iter([notice])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    # ~0.086 EUR/SEK so payable -> ~€182T, estimate -> ~€172M: a genuine
    # multi-order disagreement the scorer must flag.
    svc = _fx_svc(rate=0.0863)
    load_contracts(driver, log, "/fake/path.tar.gz", currency_svc=svc)
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    # Value is stored (not dropped) ...
    assert payload["value_eur"] > 1e14
    # ... but flagged and excluded from default aggregates.
    assert payload["value_low_confidence"] is True
    assert payload["value_quality_flag"] in (
        "implausible_magnitude", "value_disagreement",
    )
    # Cross-check signals retained.
    assert payload["estimated_value_eur"] > 0
    assert payload["value_payable_eur"] > 1e14
    # Rest of the Contract row still lands.
    assert payload["authority_id"] == "auth-1"
    assert payload["company_gmr_id"] == "company-1"


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_value_kept_when_award_is_proportional_to_estimate(
    mock_matcher_cls, mock_stream,
):
    """A multi-year HSR or defense framework can legitimately award
    €20 B with a €15 B estimate (cost overrun, scope expansion).
    Ratio is ~1.3× — well under the 1000× mismatch threshold. The
    value must pass through untouched; this is the cohort the user
    explicitly wants to see in the graph."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1", stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Big Infra GmbH"
    contractor.country = "DE"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award(currency="EUR", value=2e10)],   # €20 B awarded
        organizations={"O1": contractor},
    )
    notice.lots = [
        _lot_with_estimate("LOT-A", 1.5e10, currency="EUR"),  # €15 B est
    ]
    mock_stream.return_value = iter([notice])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    svc = _stub_currency_svc(value_eur=2e10, parsed_value=2e10)
    load_contracts(driver, log, "/fake/path.tar.gz", currency_svc=svc)
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload["value_eur"] == 2e10
    assert payload["value_original"] == 2e10


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_value_dropped_above_100b_cap_when_no_estimate(
    mock_matcher_cls, mock_stream,
):
    """Polish-style garbage with no lot estimate, so plausibility is the
    only signal. €900 B of unverifiable awarded value is implausibly
    large: STORED but flagged low-confidence (excluded from default
    aggregates), not silently dropped."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1", stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Vendor"
    contractor.country = "PL"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award(currency="PLN", value=4e12)],  # 4 T PLN
        organizations={"O1": contractor},
    )
    notice.lots = []  # no lot estimates
    mock_stream.return_value = iter([notice])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    # ~0.225 EUR/PLN -> ~€900 B awarded.
    svc = _fx_svc(rate=0.225)
    load_contracts(driver, log, "/fake/path.tar.gz", currency_svc=svc)
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload["value_eur"] > 1e11           # stored
    assert payload["value_low_confidence"] is True
    assert payload["value_quality_flag"] == "implausible_magnitude"
    assert payload["ted_notice_id"]


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_value_kept_below_100b_cap_when_no_estimate(
    mock_matcher_cls, mock_stream,
):
    """Below the absolute cap, a no-estimate award passes through.
    €50 B is implausibly large but not data-entry-error garbage; we
    surface it via the >€1 B audit log and let the DQ dashboard
    flag it for operator review rather than silently swallowing
    a potentially legitimate contract."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1", stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Defense Co"
    contractor.country = "FR"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award(currency="EUR", value=5e10)],   # €50 B
        organizations={"O1": contractor},
    )
    notice.lots = []  # no estimate to compare against
    mock_stream.return_value = iter([notice])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    svc = _stub_currency_svc(value_eur=5e10, parsed_value=5e10)
    load_contracts(driver, log, "/fake/path.tar.gz", currency_svc=svc)
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload["value_eur"] == 5e10
    assert payload["value_original"] == 5e10


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_value_dropped_at_100x_mismatch_boundary(
    mock_matcher_cls, mock_stream,
):
    """An award 150× its estimate (€1 M estimate, €150 M payable) is a
    strong disagreement. The value is stored but flagged
    value_disagreement and marked low-confidence so it is excluded from
    default aggregates."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1", stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Vendor"
    contractor.country = "DE"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award(currency="EUR", value=1.5e8)],  # €150 M awarded
        organizations={"O1": contractor},
    )
    notice.lots = [_lot_with_estimate("LOT-1", 1e6, currency="EUR")]
    mock_stream.return_value = iter([notice])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    svc = _fx_svc(rate=1.0)  # EUR; each signal converts to itself
    load_contracts(driver, log, "/fake/path.tar.gz", currency_svc=svc)
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload["value_eur"] == 1.5e8           # stored
    assert payload["value_low_confidence"] is True
    assert payload["value_quality_flag"] == "value_disagreement"
    assert payload["estimated_value_eur"] == 1e6   # estimate retained


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_aircraft_recovers_total_over_corrupted_payable(
    mock_matcher_cls, mock_stream,
):
    """The Forca Aerea fix end-to-end: a single-award notice whose
    NoticeResult TotalAmount is the clean ~€7.27 M but whose PayableAmount
    is the x1000-corrupted ~€7.27 B. The loader must store the TotalAmount
    (recovered correct value), keep the payable alongside, mark the
    payable discrepancy, and stay above the low-confidence gate."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1", stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "World Aviation"
    contractor.country = "PRT"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award(currency="EUR", value=7_274_615_930.0)],  # payable x1000
        organizations={"O1": contractor},
    )
    notice.total_value = 7_274_615.93                 # clean NoticeResult total
    notice.lots = [_lot_with_estimate("LOT-0001", 7_317_073.17)]  # estimate
    mock_stream.return_value = iter([notice])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    svc = _fx_svc(rate=1.0)
    load_contracts(driver, log, "/fake/path.tar.gz", currency_svc=svc)
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    # Recovered the true ~€7.27 M (TotalAmount), not the €7.27 B payable.
    assert abs(payload["value_eur"] - 7_274_615.93) < 1
    assert payload["value_payable_eur"] == 7_274_615_930.0
    assert payload["value_payable_discrepancy"] is True
    assert payload["value_low_confidence"] is False   # kept and counted
    assert payload["value_quality_flag"] == "ok"


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_value_kept_at_10x_overrun(
    mock_matcher_cls, mock_stream,
):
    """A 10× cost overrun (€1 M estimate → €10 M awarded) is a real
    pattern on troubled framework contracts — it must pass through
    untouched. The 100× threshold leaves a full order of magnitude
    of headroom above the worst-case legitimate overrun."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1", stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Vendor"
    contractor.country = "IT"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award(currency="EUR", value=1e7)],   # €10 M
        organizations={"O1": contractor},
    )
    notice.lots = [_lot_with_estimate("LOT-1", 1e6, currency="EUR")]
    mock_stream.return_value = iter([notice])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    svc = _stub_currency_svc(value_eur=1e7, parsed_value=1e7)
    load_contracts(driver, log, "/fake/path.tar.gz", currency_svc=svc)
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload["value_eur"] == 1e7


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_value_typical_contract_unaffected(
    mock_matcher_cls, mock_stream,
):
    """The 99.99 % case — a €207 k median contract with a
    proportional €200 k estimate — passes through without any
    modification or log noise."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1", stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Small Vendor SARL"
    contractor.country = "FR"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award(currency="EUR", value=207_117.44)],
        organizations={"O1": contractor},
    )
    notice.lots = [
        _lot_with_estimate("LOT-1", 200_000.0, currency="EUR"),
    ]
    mock_stream.return_value = iter([notice])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    svc = _stub_currency_svc(value_eur=207_117.44, parsed_value=207_117.44)
    load_contracts(driver, log, "/fake/path.tar.gz", currency_svc=svc)
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload["value_eur"] == 207_117.44
