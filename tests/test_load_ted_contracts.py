"""Tests for the TED contract loader (post-event-log)."""
from unittest.mock import MagicMock, patch

from src.etl.load_ted_contracts import load_contracts


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
    return award


def _stub_notice(*, awards, organizations):
    notice = MagicMock()
    notice.publication_number = "2025-OJS123-456789"
    notice.notice_id = "BT701/2025"
    notice.title = "Some contract"
    notice.description = "Procurement of stuff"
    notice.issue_date = "2025-09-01"
    notice.dispatch_date = "2025-09-01"
    notice.awards = awards
    notice.organizations = organizations
    notice.cpv_main = "45000000"
    notice.procedure_type = "open"
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
    assert res["authorities"] == 1
    assert res["companies"] == 1

    types = [c.args[0] for c in emit.upsert.call_args_list]
    assert types == ["UpsertAuthority", "UpsertCompany", "UpsertContract"]


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
    assert payload["company_gmr_id"] == "company-1"
    assert payload["ted_notice_id"] == "2025-OJS123-456789"
    assert payload["cpv"] == "45000000"


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
    # Authority was emitted (we resolve buyer before iterating awards).
    assert res["total"] == 0
    assert res["authorities"] == 1
    contract_emits = [
        c for c in emit.upsert.call_args_list
        if c.args[0] == "UpsertContract"
    ]
    assert contract_emits == []
