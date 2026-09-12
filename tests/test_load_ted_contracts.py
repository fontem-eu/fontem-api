"""Tests for the TED contract loader (post-event-log)."""
# pylint: disable=too-many-lines
from unittest.mock import MagicMock, patch

import pytest

from src.etl import load_ted_contracts
from src.etl.load_ted_contracts import load_contracts
from src.etl.ted_matcher import MatchResult


@pytest.fixture(autouse=True)
def _stub_should_ingest(monkeypatch):
    """Idempotency gate: default to "ingest" so the happy-path tests
    exercise the emit pipeline. Tests that want the skip path re-patch
    this to return False."""
    monkeypatch.setattr(
        "src.etl.load_ted_contracts._should_ingest",
        lambda _session, _nid, _version, _identity: True,
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


def _stub_award(currency="EUR", value=1000.0, contractor_org_id="O1",  # pylint: disable=too-many-arguments,too-many-positional-arguments
                is_winner=True, rank=None, is_consortium_member=False,
                tendering_party_id=None, lot_id="LOT-0001"):
    """One parser Award. Defaults mirror the common case (a single
    winning supplier); the multi-supplier tests pass is_winner=False /
    tendering_party_id to model named losers and consortia (parser
    0.8.0 emits one Award per named supplier)."""
    award = MagicMock()
    award.contractor_org_id = contractor_org_id
    award.value = value
    award.currency = currency
    award.award_date = "2025-09-15"
    award.conclusion_date = None
    award.tenders_received = 1  # single-bidder, for the integrity assertions
    award.is_winner = is_winner
    award.rank = rank
    award.is_consortium_member = is_consortium_member
    award.tendering_party_id = tendering_party_id
    award.lot_id = lot_id
    return award


def _stub_notice(*, awards, organizations):
    notice = MagicMock()
    # Identity as the parser reads it off the XML (eforms-parser 0.11):
    # the notice UUID and, once TED has published it, the publication
    # number. The defaults model a just-published award; tests set
    # procedure_id / notice_version / back-links as they need.
    notice.publication_number = "295342-2026"
    notice.notice_id = "912f1717-1ace-413d-aa61-cd21cd6b95e7"
    notice.procedure_id = None
    notice.notice_version = None
    notice.modifies_notice_id = None
    notice.legacy_procedure_id = None
    notice.title = "Some contract"
    notice.description = "Procurement of stuff"
    notice.issue_date = "2025-09-01"
    notice.dispatch_date = "2025-09-01"
    # TED published it three days after the buyer issued it — the gap
    # this loader has to preserve rather than collapse.
    notice.publication_date = "2025-09-04"
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
    notice.modification_value_before = None
    notice.modifies_publication_number = None
    notice.nuts = "FR101"
    notice.language = "fr"
    buyer = MagicMock()
    buyer.name = "Conseil constitutionnel"
    buyer.country = "FR"
    buyer.nuts = "FR101"
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
def test_nonpositive_tenders_received_is_withheld(
    mock_matcher_cls, mock_stream,
):
    """A bidder COUNT is >= 1 by definition; a 0/negative is corrupt
    parsing (some non-eForms notices carry it). The loader must NOT emit
    it -- a stored 0 fails values.contract_bidder_count_positive."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="auth-1",
        stub_company_gmr="company-1",
    )
    contractor = MagicMock()
    contractor.name = "Adyen N.V."
    contractor.country = "NL"
    contractor.legal_id = None
    award = _stub_award()
    award.tenders_received = 0
    mock_stream.return_value = iter([
        _stub_notice(awards=[award], organizations={"O1": contractor}),
    ])

    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    load_contracts(driver, log, "/fake/path.tar.gz")

    contract_call = next(
        c for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract"
    )
    payload = contract_call.kwargs["payload"]
    # builders.upsert_contract drops None-valued fields, so a withheld
    # count never reaches the graph.
    assert "tenders_received" not in payload


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
        "src.etl.load_ted_contracts._should_ingest",
        lambda _session, _nid, _version, _identity: False,
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
        "src.etl.load_ted_contracts._should_ingest",
        lambda _session, _nid, _version, _identity: False,  # pretend it is already in Neo4j
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

    def _fake_download(year, month, dest, package_store=None):  # pylint: disable=unused-argument
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
    lot — ratio ~1,055,124x. Quarantine policy (2026-07-06): the value
    is WITHHELD from the event — no monetary fields, quarantine marker +
    reason instead — and the claim goes to the human review queue. The
    original numbers survive in the event log and the queue snapshot;
    nothing downstream ever needs to remember a confidence flag."""
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
    queued = []
    with patch("src.etl.load_ted_contracts.value_review_queue."
               "enqueue_default", side_effect=lambda **kw: queued.append(kw) or True):
        load_contracts(driver, log, "/fake/path.tar.gz", currency_svc=svc)
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    # The value is WITHHELD — no monetary field survives the emit...
    for field in ("value_eur", "value_original", "value_currency",
                  "estimated_value_eur", "value_payable_eur"):
        assert field not in payload, field
    # ...replaced by the quarantine marker + reason.
    assert payload["value_quarantined"] is True
    assert payload["value_quarantine_reason"] == "implausible_magnitude"
    # The claim landed in the review queue with the numbers snapshot.
    assert len(queued) == 1
    assert queued[0]["claimed_value_eur"] > 1e14
    assert queued[0]["reason"] == "implausible_magnitude"
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
    # Quarantine policy (2026-07-06): essentially-certain garbage
    # (confidence < the quarantine floor) is withheld, not stored.
    assert "value_eur" not in payload
    assert payload["value_quarantined"] is True
    assert payload["value_quarantine_reason"] == "implausible_magnitude"
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


def test_emit_notice_stamps_match_provenance():
    """A resolver match's tier/confidence/layer are threaded onto the
    Contract payload so the sink can put them on the AWARDED_TO edge —
    a name_country (layer 2) match carries tier + confidence."""
    contractor = MagicMock()
    contractor.name = "AGILIS SA"
    contractor.country = "FR"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award()], organizations={"O1": contractor},
    )
    matcher = _mock_matcher("auth-1", "company-1")
    matcher.match_company.return_value = MatchResult(
        gmr_id="company-1", layer=2, confidence=0.95,
        resolver_tier="name_country",
    )
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, matcher, set(), set(), None,
    )
    payload = next(
        c for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload["match_tier"] == "name_country"
    assert payload["match_confidence"] == 0.95
    assert payload["match_layer"] == 2


def test_emit_notice_new_node_has_no_match_tier():
    """A layer-5 result minted a new node: no tier or confidence against
    an existing entity, only the layer is recorded (both are dropped by
    the builder, leaving the keys absent)."""
    contractor = MagicMock()
    contractor.name = "Totally New Vendor SARL"
    contractor.country = "FR"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award()], organizations={"O1": contractor},
    )
    matcher = _mock_matcher("auth-1", "gnew")
    matcher.match_company.return_value = MatchResult(
        gmr_id="gnew", layer=5, confidence=0.0, created_new=True,
    )
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, matcher, set(), set(), None,
    )
    payload = next(
        c for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload.get("match_tier") is None
    assert payload.get("match_confidence") is None
    assert payload["match_layer"] == 5


def test_emit_notice_stamps_modification_before_value():
    """A legacy modification's pre-modification total is converted and
    stamped as value_before_eur / value_before_original — the before->after
    delta consumers use to flag suspicious value changes. (Degraded mode:
    no currency service, so EUR proxies the original.)"""
    contractor = MagicMock()
    contractor.name = "S.C. Fortat-House S.R.L."
    contractor.country = "RO"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award()], organizations={"O1": contractor},
    )
    notice.notice_type = "can-modif"
    notice.total_value = 2925919.96              # after
    notice.modification_value_before = 2821075.49  # before
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, _mock_matcher("auth-1", "company-1"),
        set(), set(), None,
    )
    payload = next(
        c for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload["value_before_eur"] == 2821075.49
    assert payload["value_before_original"] == 2821075.49


def test_emit_notice_no_before_value_for_plain_contract():
    """A normal (non-modification) contract carries no before-value."""
    contractor = MagicMock()
    contractor.name = "Adyen N.V."
    contractor.country = "NL"
    contractor.legal_id = None
    notice = _stub_notice(
        awards=[_stub_award()], organizations={"O1": contractor},
    )
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, _mock_matcher("auth-1", "company-1"),
        set(), set(), None,
    )
    payload = next(
        c for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert "value_before_eur" not in payload
    assert "value_before_original" not in payload


# ── parties[] + contract identity (eforms-parser 0.8.0 / schemas 0.2.0) ──


def _matcher_by_name(authority_id, results_by_name):
    """A TedMatcher stand-in resolving each supplier name to its own
    MatchResult — the multi-supplier tests need per-party provenance."""
    matcher = MagicMock()
    matcher.match_authority.return_value = authority_id
    matcher.match_company.side_effect = (
        lambda name, _country, _vat=None: results_by_name[name]
    )
    return matcher


def _org(name, country="HU"):
    org = MagicMock()
    org.name = name
    org.country = country
    org.legal_id = None
    return org


def _contract_payload(emit):
    return next(
        c for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract"
    ).kwargs["payload"]


def _emit(notice):
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, _mock_matcher("auth-1", "company-1"),
        set(), set(), None,
    )
    return _contract_payload(emit)


def _vendor_notice(**overrides):
    notice = _stub_notice(
        awards=[_stub_award()], organizations={"O1": _org("Vendor Kft.")},
    )
    for k, v in overrides.items():
        setattr(notice, k, v)
    return notice


def test_contract_key_prefers_procedure_id():
    """An eForms award groups under its procedure id (BT-04), the one
    thing it shares with every later modification of the procedure."""
    payload = _emit(_vendor_notice(procedure_id="PROC-7"))
    assert payload["contract_key"] == "PROC-7"
    assert payload["procedure_id"] == "PROC-7"
    assert payload["notice_kind"] == "award"


def test_contract_key_award_falls_back_to_publication_number():
    """No procedure id (a legacy award): its own publication number."""
    payload = _emit(_vendor_notice(publication_number="24047-2024"))
    assert payload["contract_key"] == "24047-2024"
    assert payload["ted_publication_number"] == "24047-2024"
    assert payload["notice_kind"] == "award"


def test_contract_key_legacy_modification_uses_modifies_ref():
    """A legacy F20 modification groups under the publication number of
    the award it modifies, never its own — otherwise every modification
    would become its own 'contract' and totals double-count."""
    payload = _emit(_vendor_notice(
        notice_type="can-modif", publication_number="555-2026",
        modifies_publication_number="111-2024",
    ))
    assert payload["contract_key"] == "111-2024"
    assert payload["notice_kind"] == "modification"
    assert payload["notice_type"] == "can-modif"
    assert payload["modifies_publication_number"] == "111-2024"
    assert "modifies_notice_id" not in payload


def test_contract_key_eforms_modification_is_its_procedure_id():
    """An eForms modification keys on its procedure id like its award;
    the back-link still travels so the sink can resolve the chain (and
    adopt the award's entity when that award was keyed differently)."""
    payload = _emit(_vendor_notice(
        notice_type="can-modif", procedure_id="PROC-9",
        modifies_publication_number="549184-2020",
    ))
    assert payload["contract_key"] == "PROC-9"
    assert payload["modifies_publication_number"] == "549184-2020"


def test_modification_back_link_as_notice_id_travels_on_payload():
    """The buyer wrote the back-link as '<uuid>-01': the parser hands
    over the bare uuid and the loader passes it through untouched."""
    payload = _emit(_vendor_notice(
        notice_type="can-modif", procedure_id="PROC-9",
        modifies_notice_id="a64a67f4-a562-4014-ae25-232da2f4fa1c",
    ))
    assert payload["modifies_notice_id"] == "a64a67f4-a562-4014-ae25-232da2f4fa1c"
    assert "modifies_publication_number" not in payload


def test_award_never_carries_back_links():
    payload = _emit(_vendor_notice(
        modifies_publication_number="111-2024", modifies_notice_id="x",
    ))
    assert "modifies_publication_number" not in payload
    assert "modifies_notice_id" not in payload


def test_contract_key_falls_back_to_notice_uuid():
    """No identity on the XML at all (TED has not published it yet and
    there is no procedure id): the notice UUID is the last resort."""
    payload = _emit(_vendor_notice(publication_number=None))
    assert payload["contract_key"] == "912f1717-1ace-413d-aa61-cd21cd6b95e7"
    assert "ted_publication_number" not in payload


def test_identity_stamps_travel_on_payload():
    payload = _emit(_vendor_notice(
        notice_version="01", legacy_procedure_id="EKR001152382021",
    ))
    assert payload["notice_version"] == "01"
    assert payload["legacy_procedure_id"] == "EKR001152382021"


def test_legacy_notice_keys_on_publication_number():
    """A legacy TED_EXPORT notice's notice_id is the human OJS reference
    ('2024/S 010-024047'); the Contract IRI and ted_notice_id key on the
    publication number the parser read from the same XML, so the key is
    the same whichever way the notice was discovered."""
    payload = _emit(_vendor_notice(
        notice_id="2024/S 010-024047", publication_number="24047-2024",
    ))
    assert payload["ted_notice_id"] == "24047-2024"
    assert payload["contract_key"] == "24047-2024"


def test_parties_mixed_winner_loser_consortium():
    """A HU-style eForms notice: a two-member winning consortium plus a
    named losing bidder. Every supplier is resolved and listed; the
    top-level company/match fields stay the primary winner's; the
    contract value counts the consortium's undivided value ONCE and the
    loser's bid never."""
    awards = [
        _stub_award(value=1000.0, contractor_org_id="O1",
                    is_consortium_member=True, tendering_party_id="TPA-1"),
        _stub_award(value=1000.0, contractor_org_id="O2",
                    is_consortium_member=True, tendering_party_id="TPA-1"),
        _stub_award(value=800.0, contractor_org_id="O3", is_winner=False,
                    rank=2, tendering_party_id="TPA-2"),
    ]
    notice = _stub_notice(
        awards=awards,
        organizations={"O1": _org("Alfa Zrt."), "O2": _org("Beta Kft."),
                       "O3": _org("Gamma Bt.")},
    )
    notice.total_value = 1000.0
    matcher = _matcher_by_name("auth-1", {
        "Alfa Zrt.": MatchResult(gmr_id="gmr-alfa", layer=2, confidence=0.99,
                                 resolver_tier="vat"),
        "Beta Kft.": MatchResult(gmr_id="gmr-beta", layer=3, confidence=0.92,
                                 resolver_tier="fuzzy"),
        "Gamma Bt.": MatchResult(gmr_id="gmr-gamma", layer=5, confidence=0.0,
                                 created_new=True),
    })
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, matcher, set(), set(), None,
    )
    payload = _contract_payload(emit)

    # Backward compat: top-level fields are the primary winner's.
    assert payload["company_gmr_id"] == "gmr-alfa"
    assert payload["match_tier"] == "vat"
    assert payload["match_confidence"] == 0.99
    assert payload["match_layer"] == 2

    # parties[]: all three suppliers, with roles + per-party provenance.
    parties = payload["parties"]
    by_gmr = {p["company_gmr_id"]: p for p in parties}
    assert set(by_gmr) == {"gmr-alfa", "gmr-beta", "gmr-gamma"}
    assert by_gmr["gmr-alfa"]["role"] == "winner"
    assert by_gmr["gmr-alfa"]["is_consortium_member"] is True
    assert by_gmr["gmr-alfa"]["tendering_party_id"] == "TPA-1"
    assert by_gmr["gmr-beta"]["role"] == "winner"
    assert by_gmr["gmr-beta"]["match_tier"] == "fuzzy"
    assert by_gmr["gmr-beta"]["match_confidence"] == 0.92
    assert by_gmr["gmr-gamma"]["role"] == "named_tenderer"
    assert by_gmr["gmr-gamma"]["rank"] == 2
    # created_new: no tier/confidence against an existing entity.
    assert "match_tier" not in by_gmr["gmr-gamma"]
    assert "match_confidence" not in by_gmr["gmr-gamma"]
    assert by_gmr["gmr-gamma"]["match_layer"] == 5

    # Value correctness: the consortium's shared 1000 counts once (not
    # 2000) and the loser's 800 bid is excluded entirely.
    assert payload["value_eur"] == 1000.0
    assert payload["value_payable_eur"] == 1000.0
    # tenders_received stays the published bidder count — never len(parties).
    assert payload["tenders_received"] == 1

    # Every named supplier got the create-if-not-found UpsertCompany path.
    company_ids = [
        c.kwargs["payload"]["gmr_id"] for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertCompany"
    ]
    assert sorted(company_ids) == ["gmr-alfa", "gmr-beta", "gmr-gamma"]


def test_value_excludes_loser_bids():
    """A named tenderer's Award.value is its BID amount. Summing it into
    the contract value would fabricate money — winner-only."""
    awards = [
        _stub_award(value=500.0, contractor_org_id="O1"),
        _stub_award(value=9_999_999.0, contractor_org_id="O2",
                    is_winner=False, tendering_party_id="TPB-9"),
    ]
    notice = _stub_notice(
        awards=awards,
        organizations={"O1": _org("Winner Kft."), "O2": _org("Loser Zrt.")},
    )
    notice.total_value = 500.0
    matcher = _matcher_by_name("auth-1", {
        "Winner Kft.": MatchResult(gmr_id="gmr-w", layer=2, confidence=0.99,
                                   resolver_tier="vat"),
        "Loser Zrt.": MatchResult(gmr_id="gmr-l", layer=2, confidence=0.97,
                                  resolver_tier="name_country"),
    })
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, matcher, set(), set(), None,
    )
    payload = _contract_payload(emit)
    assert payload["value_eur"] == 500.0
    assert payload["value_payable_eur"] == 500.0
    roles = {p["company_gmr_id"]: p["role"] for p in payload["parties"]}
    assert roles == {"gmr-w": "winner", "gmr-l": "named_tenderer"}


def test_multi_winner_total_not_attributed():
    """Two winning parties on different lots: the notice-level
    TotalAmount is an aggregate we cannot split, so it is dropped and
    the per-party payables sum instead (100 + 200, never the 999)."""
    lot_a, lot_b = MagicMock(), MagicMock()
    lot_a.lot_id, lot_a.estimated_value = "LOT-A", 100.0
    lot_b.lot_id, lot_b.estimated_value = "LOT-B", 200.0
    awards = [
        _stub_award(value=100.0, contractor_org_id="O1", lot_id="LOT-A"),
        _stub_award(value=200.0, contractor_org_id="O2", lot_id="LOT-B"),
    ]
    notice = _stub_notice(
        awards=awards,
        organizations={"O1": _org("Uno Kft."), "O2": _org("Duo Kft.")},
    )
    notice.lots = [lot_a, lot_b]
    notice.total_value = 999.0
    matcher = _matcher_by_name("auth-1", {
        "Uno Kft.": MatchResult(gmr_id="gmr-1", layer=2, confidence=0.99,
                                resolver_tier="vat"),
        "Duo Kft.": MatchResult(gmr_id="gmr-2", layer=2, confidence=0.99,
                                resolver_tier="vat"),
    })
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, matcher, set(), set(), None,
    )
    payload = _contract_payload(emit)
    assert payload["value_payable_eur"] == 300.0
    assert payload["value_eur"] == 300.0
    assert payload["estimated_value_eur"] == 300.0


def test_no_winner_notice_emits_named_tenderers_only():
    """SettledContract references that resolve no winner: the notice
    still lands (its named tenderers matter) but carries no company
    attribution and no awarded value — a loser's bid must not become
    the contract value."""
    awards = [
        _stub_award(value=700.0, contractor_org_id="O1", is_winner=False),
    ]
    notice = _stub_notice(
        awards=awards, organizations={"O1": _org("Solo Bt.")},
    )
    notice.total_value = None
    matcher = _matcher_by_name("auth-1", {
        "Solo Bt.": MatchResult(gmr_id="gmr-s", layer=2, confidence=0.95,
                                resolver_tier="name_country"),
    })
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, matcher, set(), set(), None,
    )
    payload = _contract_payload(emit)
    assert "company_gmr_id" not in payload
    assert "value_eur" not in payload
    assert "value_payable_eur" not in payload
    assert payload["parties"][0]["role"] == "named_tenderer"
    # The loser is still resolved + minted (create-if-not-found).
    assert any(c.args[0] == "UpsertCompany"
               for c in emit.upsert.call_args_list)


def test_parties_dedupe_supplier_winning_several_lots():
    """A supplier that won two lots is one party entry (and one
    UpsertCompany), not two."""
    awards = [
        _stub_award(value=100.0, contractor_org_id="O1", lot_id="LOT-A"),
        _stub_award(value=200.0, contractor_org_id="O1", lot_id="LOT-B"),
    ]
    notice = _stub_notice(
        awards=awards, organizations={"O1": _org("Repeat Kft.")},
    )
    matcher = _matcher_by_name("auth-1", {
        "Repeat Kft.": MatchResult(gmr_id="gmr-r", layer=2, confidence=0.99,
                                   resolver_tier="vat"),
    })
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, matcher, set(), set(), None,
    )
    payload = _contract_payload(emit)
    assert len(payload["parties"]) == 1
    assert payload["parties"][0]["company_gmr_id"] == "gmr-r"
    companies = [c for c in emit.upsert.call_args_list
                 if c.args[0] == "UpsertCompany"]
    assert len(companies) == 1


def test_legacy_single_contractor_path_unchanged():
    """Oldgen/F03 regression: a single CONTRACTOR award (parser default
    is_winner=True, no rank / tendering party / consortium flags)
    produces the same top-level fields as before, and its parties[] is
    exactly one plain winner entry — no named_tenderer entries, no
    spurious rank/consortium keys."""
    award = _stub_award(rank=None, tendering_party_id=None,
                        is_consortium_member=False, lot_id=None)
    notice = _stub_notice(
        awards=[award], organizations={"O1": _org("S.C. Fortat-House S.R.L.",
                                                  country="RO")},
    )
    notice.total_value = 1000.0
    notice.notice_id = "2024/S 010-024047"
    notice.publication_number = "24047-2024"
    matcher = _mock_matcher("auth-1", "company-1")
    matcher.match_company.return_value = MatchResult(
        gmr_id="company-1", layer=2, confidence=0.95,
        resolver_tier="name_country",
    )
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, matcher, set(), set(), None,
    )
    payload = _contract_payload(emit)
    # Top-level shape is byte-for-byte the pre-parties contract.
    assert payload["ted_notice_id"] == "24047-2024"
    assert payload["company_gmr_id"] == "company-1"
    assert payload["match_tier"] == "name_country"
    assert payload["match_confidence"] == 0.95
    assert payload["match_layer"] == 2
    assert payload["value_eur"] == 1000.0
    assert payload["tenders_received"] == 1
    # parties[] is the single plain winner.
    assert payload["parties"] == [{
        "company_gmr_id": "company-1",
        "name": "S.C. Fortat-House S.R.L.",
        "role": "winner",
        "match_tier": "name_country",
        "match_confidence": 0.95,
        "match_layer": 2,
    }]


def test_emitted_payload_validates_against_schema():
    """Smoke emit: the full multi-supplier payload passes the same
    jsonschema validation the producer runs at emit time (the event
    would be rejected before landing otherwise)."""
    # pylint: disable=import-outside-toplevel
    from fontem_event_schemas.validate import validate

    awards = [
        _stub_award(value=1000.0, contractor_org_id="O1",
                    is_consortium_member=True, tendering_party_id="TPA-1"),
        _stub_award(value=800.0, contractor_org_id="O2", is_winner=False,
                    rank=2, tendering_party_id="TPA-2"),
    ]
    notice = _stub_notice(
        awards=awards,
        organizations={"O1": _org("Alfa Zrt."), "O2": _org("Beta Kft.")},
    )
    notice.total_value = 1000.0
    matcher = _matcher_by_name(
        "11111111-2222-5333-8444-555555555555", {
            "Alfa Zrt.": MatchResult(
                gmr_id="00040372-dad6-5d34-882c-8b8624b4e734", layer=2,
                confidence=0.99, resolver_tier="vat"),
            "Beta Kft.": MatchResult(
                gmr_id="00040372-dad6-5d34-882c-8b8624b4e735", layer=5,
                confidence=0.0, created_new=True),
        })
    notice.procedure_id = "PROC-1"
    notice.notice_version = "01"
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, matcher, set(), set(), None,
    )
    for call in emit.upsert.call_args_list:
        event_type = call.args[0]
        validate(event_type, 1, call.kwargs["payload"])
    payload = _contract_payload(emit)
    assert payload["contract_key"] == "PROC-1"
    assert len(payload["parties"]) == 2


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_forwards_nuts_on_authority_and_contract(
    mock_matcher_cls, mock_stream,
):
    """Authority + contract payloads carry the parser-extracted NUTS.
    The previous code path read notice.place_nuts (a name never set by
    the parser) and passed nothing at all for the authority — so every
    downstream search.entity_embeddings row landed with nuts=NULL."""
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
    load_contracts(driver, log, "/fake/path.tar.gz")

    by_type = {c.args[0]: c for c in emit.upsert.call_args_list}
    assert by_type["UpsertAuthority"].kwargs["payload"].get("nuts") == "FR101"
    assert by_type["UpsertContract"].kwargs["payload"].get("nuts") == "FR101"

@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_milli_euro_leak_rescaled_at_emit(
    mock_matcher_cls, mock_stream,
):
    """A notice whose award total is ~x1000 its own estimate gets its
    monetary fields rescaled /1000 before scoring, and the emitted
    payload carries the value_scale_corrected marker."""
    mock_matcher_cls.return_value = _mock_matcher(
        stub_authority_id="11111111-2222-5333-8444-555555555555",
        stub_company_gmr="00040372-dad6-5d34-882c-8b8624b4e734",
    )
    contractor = MagicMock()
    contractor.name = "Veiga Lopes SA"
    contractor.country = "PT"
    contractor.legal_id = MagicMock(value="503079235", scheme_name="VAT")
    award = _stub_award()
    award.value = 9281922790.0          # milli-euro leak (x1000)
    notice = _stub_notice(
        awards=[award], organizations={"O1": contractor},
    )
    notice.total_value = 9281922790.0
    # the estimate signal comes from the awarded lot's
    # EstimatedOverallContractAmount — give it the SANE value so the
    # x1000 ratio evidence fires (the aircraft-case shape)
    lot = MagicMock()
    lot.lot_id = "LOT-0001"
    lot.estimated_value = 9289549.17
    notice.lots = [lot]

    mock_stream.return_value = iter([notice])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    load_contracts(driver, log, "/fake/path.tar.gz")

    by_type = {c.args[0]: c for c in emit.upsert.call_args_list}
    payload = by_type["UpsertContract"].kwargs["payload"]
    assert payload.get("value_scale_corrected") in ("ratio", "country_prior")
    assert payload.get("value_eur") == 9281922.79


# ── Notice publication date ────────────────────────────────────────────────


def _notice_with_contractor():
    """A stub notice that actually resolves a winner, so a Contract is emitted."""
    contractor = MagicMock()
    contractor.name = "S.C. Fortat-House S.R.L."
    contractor.country = "RO"
    contractor.legal_id = None
    return _stub_notice(awards=[_stub_award()], organizations={"O1": contractor})


def test_as_day_strips_ted_timezone_offset():
    """TED sends '2026-08-25+02:00'; the graph stores bare ISO days."""
    as_day = load_ted_contracts._as_day  # pylint: disable=protected-access
    assert as_day("2026-08-25+02:00") == "2026-08-25"
    assert as_day("2026-08-25Z") == "2026-08-25"
    assert as_day("2026-08-25") == "2026-08-25"


def test_as_day_rejects_non_dates_and_sentinels():
    """Anything that is not a real ISO day is dropped, not coerced.

    A ten-character non-date (a Mock repr, a stray identifier) must not
    become a publication date: it would sort into the middle of the range
    index and silently corrupt every feed window reading this field.
    """
    as_day = load_ted_contracts._as_day  # pylint: disable=protected-access
    assert as_day(None) is None
    assert as_day("") is None
    assert as_day("<MagicMock") is None
    assert as_day("not-a-date") is None
    assert as_day("2026-13-45") is None
    assert as_day(20260825) is None
    assert as_day("2000-01-01") is None  # TED's 'unknown' sentinel
    assert as_day("1900-01-01") is None


def test_emit_notice_falls_back_to_parsed_publication_date():
    """No search record (bulk archive path): use efbc:PublicationDate."""
    notice = _notice_with_contractor()
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, _mock_matcher("auth-1", "company-1"),
        set(), set(), None,
    )
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload["publication_date"] == "2025-09-04"


def test_emit_notice_falls_back_to_issue_date_when_unpublished():
    """Pre-eForms notices carry neither; issue_date is the last resort."""
    notice = _notice_with_contractor()
    notice.publication_date = None
    emit = MagicMock()
    load_ted_contracts._emit_notice(  # pylint: disable=protected-access
        notice, emit, _mock_matcher("auth-1", "company-1"),
        set(), set(), None,
    )
    payload = next(
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ).kwargs["payload"]
    assert payload["publication_date"] == "2025-09-01"


# ── one ingest path: the idempotency gate and the two discovery paths ──


def _session_with_state(row):
    """A Neo4j session stand-in whose single() returns ``row`` (a dict
    shaped like _INGEST_STATE's RETURN) or None."""
    session = MagicMock()
    session.run.return_value.single.return_value = row
    return session


def _state(present=True, version=None, has_procedure_id=True,
           has_publication_number=True):
    return {
        "present": present, "version": version,
        "has_procedure_id": has_procedure_id,
        "has_publication_number": has_publication_number,
    }


def test_should_ingest_when_absent(monkeypatch):
    monkeypatch.undo()
    should = load_ted_contracts._should_ingest  # pylint: disable=protected-access
    assert should(_session_with_state(None), "n1", "01", "procedure_id")
    assert should(_session_with_state(_state(present=False)), "n1", "01",
                  "procedure_id")


def test_should_ingest_skips_same_or_older_version(monkeypatch):
    monkeypatch.undo()
    should = load_ted_contracts._should_ingest  # pylint: disable=protected-access
    assert not should(_session_with_state(_state(version="01")), "n1", "01",
                      "procedure_id")
    # search API hands the version over as an int; XML as "01"
    assert not should(_session_with_state(_state(version="02")), "n1", 2,
                      "procedure_id")
    assert not should(_session_with_state(_state(version="02")), "n1", "01",
                      "procedure_id")
    # neither side versioned (legacy): present + stamped is enough
    assert not should(_session_with_state(_state()), "n1", None,
                      "ted_publication_number")


def test_should_ingest_newer_version(monkeypatch):
    monkeypatch.undo()
    should = load_ted_contracts._should_ingest  # pylint: disable=protected-access
    assert should(_session_with_state(_state(version="01")), "n1", "02",
                  "procedure_id")
    assert should(_session_with_state(_state(version=1)), "n1", "02",
                  "procedure_id")


def test_should_ingest_restamps_node_loaded_before_identity(monkeypatch):
    """The repair is a re-run: a node the old archive path wrote without
    a procedure id (or a legacy one without its publication number) is
    ingested again, even at the same version."""
    monkeypatch.undo()
    should = load_ted_contracts._should_ingest  # pylint: disable=protected-access
    assert should(_session_with_state(_state(version="01", has_procedure_id=False)),
                  "n1", "01", "procedure_id")
    assert should(_session_with_state(_state(has_publication_number=False)),
                  "24047-2024", None, "ted_publication_number")
    # ...but a legacy node lacking a procedure id is not "unstamped":
    # legacy notices never have one.
    assert not should(_session_with_state(_state(has_procedure_id=False)),
                      "24047-2024", None, "ted_publication_number")
    # a notice with no identity at all cannot be re-stamped: skip
    assert not should(_session_with_state(_state(has_procedure_id=False,
                                                 has_publication_number=False)),
                      "n1", None, None)


def test_notice_key_and_identity_property():
    notice = MagicMock()
    notice.notice_id = "2022/S 081-217109"
    notice.publication_number = "217109-2022"
    assert load_ted_contracts.notice_key(notice) == "217109-2022"
    notice.notice_id = "912f1717-1ace-413d-aa61-cd21cd6b95e7"
    assert load_ted_contracts.notice_key(notice) == notice.notice_id
    identity = load_ted_contracts._identity_property  # pylint: disable=protected-access
    assert identity("PROC", "1-2026") == "procedure_id"
    assert identity(None, "1-2026") == "ted_publication_number"
    assert identity(None, None) is None


def test_version_num_normalises_xml_and_search_forms():
    v = load_ted_contracts._version_num  # pylint: disable=protected-access
    assert v("01") == 1 and v(1) == 1 and v("12") == 12
    assert v(None) is None and v("") is None and v("x") is None


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_archive_path_ingests_modifications_too(mock_matcher_cls, mock_stream):
    """A modification found in a monthly archive keys its contract the
    same way one found through the search API does — both go through
    ingest_notice — so the archive path can no longer produce an award
    the modification cannot join."""
    mock_matcher_cls.return_value = _mock_matcher("auth-1", "company-1")
    award = _vendor_notice(procedure_id="PROC-1", notice_version="01")
    award.notice_id = "11111111-1111-1111-1111-111111111111"
    modification = _vendor_notice(
        procedure_id="PROC-1", notice_type="can-modif", notice_version="01",
        modifies_notice_id="11111111-1111-1111-1111-111111111111",
    )
    modification.notice_id = "22222222-2222-2222-2222-222222222222"
    other = _vendor_notice(notice_type="cn-standard")  # a call: not loaded
    mock_stream.return_value = iter([award, modification, other])
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    res = load_contracts(driver, log, "/fake/path.tar.gz")

    assert res["total"] == 2
    payloads = [c.kwargs["payload"] for c in emit.upsert.call_args_list
                if c.args[0] == "UpsertContract"]
    assert [p["notice_kind"] for p in payloads] == ["award", "modification"]
    assert {p["contract_key"] for p in payloads} == {"PROC-1"}


@patch("src.etl.load_ted_contracts.TedRawStore")
@patch("src.etl.load_ted_contracts.parse_notice_xml")
@patch("src.etl.load_ted_contracts.ted_search")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_search_path_is_discovery_only(mock_matcher_cls, mock_search,
                                       mock_parse, mock_raw_store, monkeypatch):
    """The search record locates the XML; identity comes from the parsed
    notice. A record whose stamps disagree with the XML changes nothing
    on the event, and the pre-download skip uses the record's
    identifier + version."""
    from datetime import date  # pylint: disable=import-outside-toplevel
    mock_matcher_cls.return_value = _mock_matcher("auth-1", "company-1")
    mock_raw_store.from_env.return_value = None
    mock_search.NOTICE_TYPES = ("can-standard", "can-modif")
    mock_search.SEARCH_TIMEOUT = 1
    mock_search.search_day.return_value = iter([
        {"notice-identifier": "912f1717-1ace-413d-aa61-cd21cd6b95e7",
         "publication-number": "999999-2026",  # record lies
         "notice-version": 1, "notice-type": "can-standard",
         "procedure-identifier": "RECORD-PROC",
         "publication-date": "2026-01-01+01:00",
         "links": {"xml": {"MUL": "https://ted/x.xml"}}},
        {"notice-identifier": "skip-me", "notice-version": 1,
         "procedure-identifier": "P", "publication-number": "1-2026",
         "links": {"xml": {"MUL": "https://ted/y.xml"}}},
    ])
    mock_search.xml_url.side_effect = lambda rec: rec["links"]["xml"]["MUL"]
    mock_search.fetch_xml.return_value = b"<xml/>"
    mock_parse.return_value = _vendor_notice(
        procedure_id="XML-PROC", notice_version="01",
    )
    seen = []

    def _gate(_session, nid, version, identity):
        seen.append((nid, version, identity))
        return nid != "skip-me"
    monkeypatch.setattr("src.etl.load_ted_contracts._should_ingest", _gate)

    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    totals = load_ted_contracts.load_contracts_incremental(
        driver, log, date(2026, 1, 1), date(2026, 1, 1),
    )

    assert totals["emitted"] == 1 and totals["skipped"] == 1
    assert mock_search.fetch_xml.call_count == 1  # skipped before download
    assert seen[0] == ("912f1717-1ace-413d-aa61-cd21cd6b95e7", 1, "procedure_id")
    payload = _contract_payload(emit)
    assert payload["contract_key"] == "XML-PROC"
    assert payload["ted_publication_number"] == "295342-2026"
    assert payload["publication_date"] == "2025-09-04"



# ── resilience: transient Neo4j errors and bad notices ─────────────


def test_transient_neo4j_error_is_retried(monkeypatch):
    """BookmarkTimeout under a busy sink ended the first 2026-06 range Job
    in fontem-shared after 16 minutes. Transient errors are retried with
    backoff; the notice is emitted on the attempt that succeeds."""
    from neo4j.exceptions import TransientError  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(load_ted_contracts.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _flaky(_session, _nid, _version, _identity):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("Neo.TransientError.Transaction.BookmarkTimeout")
        return True
    monkeypatch.setattr("src.etl.load_ted_contracts._should_ingest", _flaky)
    log, emit = _mock_log()
    ctx = load_ted_contracts.IngestContext(matcher=_mock_matcher("auth-1", "company-1"))
    out = load_ted_contracts.ingest_notice(_vendor_notice(), MagicMock(), log, ctx)
    assert out == "emitted"
    assert calls["n"] == 3
    assert any(c.args[0] == "UpsertContract" for c in emit.upsert.call_args_list)


def test_transient_neo4j_error_gives_up_after_retries(monkeypatch):
    from neo4j.exceptions import TransientError  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(load_ted_contracts.time, "sleep", lambda _s: None)

    def _always(_session, _nid, _version, _identity):
        raise TransientError("Neo.TransientError.Transaction.BookmarkTimeout")
    monkeypatch.setattr("src.etl.load_ted_contracts._should_ingest", _always)
    log, _emit = _mock_log()
    ctx = load_ted_contracts.IngestContext(matcher=_mock_matcher("auth-1", "company-1"))
    with pytest.raises(TransientError):
        load_ted_contracts.ingest_notice(_vendor_notice(), MagicMock(), log, ctx)


@patch("src.etl.load_ted_contracts.stream_notices")
@patch("src.etl.load_ted_contracts.TedMatcher")
def test_archive_run_counts_a_failed_notice_and_continues(
    mock_matcher_cls, mock_stream, monkeypatch,
):
    """A notice that fails for good is logged and counted; the range
    Job goes on with the next one instead of dying days in."""
    mock_matcher_cls.return_value = _mock_matcher("auth-1", "company-1")
    bad = _vendor_notice()
    bad.notice_id = "bad-1"
    good = _vendor_notice()
    good.notice_id = "good-1"
    mock_stream.return_value = iter([bad, good])
    real = load_ted_contracts.ingest_notice

    def _explode_on_bad(notice, session, log, ctx):
        if notice.notice_id == "bad-1":
            raise RuntimeError("parser choked")
        return real(notice, session, log, ctx)
    monkeypatch.setattr("src.etl.load_ted_contracts.ingest_notice", _explode_on_bad)
    driver, _session = _mock_driver_and_session()
    log, emit = _mock_log()
    res = load_contracts(driver, log, "/fake/path.tar.gz")
    assert res["errors"] == 1 and res["total"] == 1
    assert sum(1 for c in emit.upsert.call_args_list if c.args[0] == "UpsertContract") == 1
