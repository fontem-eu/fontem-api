"""The loader with the cleaning stage in it (data-backlog Part 5).

A separate module from ``test_load_ted_contracts.py`` only because that
file is already at pylint's line limit. The stubs follow the same
conventions.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.etl.cleaning import CleaningReport, Lookups
from src.etl.cleaning.lookups import InMemoryPeerStats, PeerBand
from src.etl.dry_run_log import DryRunEventLog
from src.etl.load_ted_contracts import (  # pylint: disable=protected-access
    FRAMEWORKS_ENV, frameworks_enabled, load_contracts,
)

from .test_load_ted_contracts import (  # reuse the house stubs
    _mock_driver_and_session, _mock_log, _mock_matcher, _stub_award, _stub_notice,
)

ITALIAN_JUNK = (
    "Gara aggiudicata come da determina n. 543 del 2013 pubblicata sul "
    "sito www.csc.sanita.fvg.it alla sezione delibere e decreti."
)
AUTH_ID = "11111111-2222-5333-8444-555555555555"
COMPANY_ID = "00040372-dad6-5d34-882c-8b8624b4e734"


@pytest.fixture(autouse=True)
def _stub_should_ingest(monkeypatch):
    monkeypatch.setattr(
        "src.etl.load_ted_contracts._should_ingest",
        lambda _session, _nid, _version, _identity: True,
    )


def _org(name, *, country="IT", legal_id=None, scheme=None):
    org = MagicMock()
    org.name = name
    org.country = country
    org.legal_id = (MagicMock(value=legal_id, scheme_name=scheme)
                    if legal_id is not None else None)
    return org


def _payloads(emit, event_type):
    return [c.kwargs["payload"] for c in emit.upsert.call_args_list
            if c.args[0] == event_type]


def _run(notice, *, lookups=None, report=None, dry_run=False,  # pylint: disable=too-many-arguments
         emit_frameworks=False, log=None, matcher=None):
    driver, _ = _mock_driver_and_session()
    if log is None:
        log, emit = _mock_log()
    else:
        emit = None
    with patch("src.etl.load_ted_contracts.stream_notices",
               return_value=iter([notice])), \
         patch("src.etl.load_ted_contracts.TedMatcher",
               return_value=matcher or _mock_matcher(AUTH_ID, COMPANY_ID)) as m:
        result = load_contracts(driver, log, "/fake/path.tar.gz",
                                lookups=lookups, report=report,
                                dry_run=dry_run, emit_frameworks=emit_frameworks)
    return result, emit, m.return_value


class TestWithheldSupplier:
    """A supplier the cleaning stage refused must leave no trace that
    could become a :Company - the sink stubs one for any gmr_id it sees
    without an UpsertCompany."""

    def _notice(self):
        return _stub_notice(awards=[_stub_award()],
                            organizations={"O1": _org(ITALIAN_JUNK)})

    def test_no_company_is_created_for_it(self):
        _, emit, _ = _run(self._notice())
        types = [c.args[0] for c in emit.upsert.call_args_list]
        assert "UpsertCompany" not in types
        assert types == ["UpsertAuthority", "UpsertContract"]

    def test_it_is_absent_from_the_parties_and_the_top_level_company(self):
        _, emit, _ = _run(self._notice())
        [contract] = _payloads(emit, "UpsertContract")
        assert contract.get("parties") in (None, [])
        assert contract.get("company_gmr_id") is None

    def test_the_raw_text_travels_as_a_withheld_supplier(self):
        _, emit, _ = _run(self._notice())
        [contract] = _payloads(emit, "UpsertContract")
        assert contract["suppliers_withheld"] == [{
            "name_raw": ITALIAN_JUNK,
            "reason": "it.notice_text_in_supplier_name",
            "role": "winner",
            "org_id": "O1",
        }]

    def test_the_contract_says_which_rules_fired(self):
        _, emit, _ = _run(self._notice())
        [contract] = _payloads(emit, "UpsertContract")
        assert "it.notice_text_in_supplier_name" in contract["cleaning_rules"]

    def test_the_matcher_is_never_asked_about_it(self):
        """Withholding happens before identity: no gmr_id is minted for
        a name the rules rejected."""
        _, _, matcher = _run(self._notice())
        matcher.match_company.assert_not_called()

    def test_a_real_supplier_on_the_same_notice_is_unaffected(self):
        notice = _stub_notice(
            awards=[_stub_award(contractor_org_id="O1"),
                    _stub_award(contractor_org_id="O2", lot_id="LOT-0002")],
            organizations={"O1": _org(ITALIAN_JUNK),
                           "O2": _org("Alfa S.p.A.", legal_id="IT00123456789",
                                      scheme="VAT")},
        )
        _, emit, _ = _run(notice)
        assert len(_payloads(emit, "UpsertCompany")) == 1
        [contract] = _payloads(emit, "UpsertContract")
        assert [p["name"] for p in contract["parties"]] == ["Alfa S.p.A."]
        assert [w["org_id"] for w in contract["suppliers_withheld"]] == ["O1"]


class TestIdentifierNormalisation:
    """C3: the one rule that runs before identity, because the matcher
    is what turns an identifier into a gmr_id."""

    def test_a_bare_portuguese_nif_reaches_the_matcher_as_a_vat(self):
        notice = _stub_notice(
            awards=[_stub_award()],
            organizations={"O1": _org("Visualforma, S.A.", country="PRT",
                                      legal_id="503536717")},
        )
        _, _, matcher = _run(notice)
        assert matcher.match_company.call_args.args == (
            "Visualforma, S.A.", "PRT", "PT503536717")

    def test_a_german_leitweg_id_is_not_turned_into_a_vat(self):
        notice = _stub_notice(
            awards=[_stub_award()],
            organizations={"O1": _org("Bundesdruckerei GmbH", country="DEU",
                                      legal_id="053660036036-31001-86")},
        )
        _, _, matcher = _run(notice)
        assert matcher.match_company.call_args.args[2] is None

    def test_an_already_canonical_vat_is_passed_through_unchanged(self):
        notice = _stub_notice(
            awards=[_stub_award()],
            organizations={"O1": _org("Adyen N.V.", country="NL",
                                      legal_id="NL850456592B01", scheme="VAT")},
        )
        _, emit, matcher = _run(notice)
        assert matcher.match_company.call_args.args[2] == "NL850456592B01"
        [contract] = _payloads(emit, "UpsertContract")
        assert "generic.national_id_country_prefixed" not in (
            contract.get("cleaning_rules") or [])


class TestRawSignalsOnThePayload:
    def test_the_published_text_behind_the_cleaned_fields_travels(self):
        notice = _stub_notice(awards=[_stub_award()],
                              organizations={"O1": _org("Alfa S.p.A.")})
        notice.notice_language = "POR"
        notice.customization_id = "eforms-sdk-1.14"
        notice.tender_result_award_date_raw = "2000-01-01+01:00"
        notice.awards[0].value_raw = "24474133"
        notice.awards[0].award_date_raw = "2000-01-01"
        notice.awards[0].tender_reference = "0.0"
        _, emit, _ = _run(notice)
        [contract] = _payloads(emit, "UpsertContract")
        assert contract["notice_language"] == "POR"
        assert contract["eforms_sdk"] == "eforms-sdk-1.14"
        assert contract["tender_result_award_date_raw"] == "2000-01-01+01:00"
        # verbatim text with its currency, per the 0.9.0 schema
        assert contract["value_raw"] == "24474133 EUR"
        assert contract["award_date_raw"] == "2000-01-01"
        assert contract["tender_reference"] == "0.0"
        assert "generic.date_placeholder" in contract["cleaning_rules"]


class TestValueQuarantine:
    def _notice(self, value):
        buyer = MagicMock()
        buyer.name = "Municipio de Beja"
        buyer.country = "PT"
        buyer.nuts = "PT184"
        buyer.legal_id = MagicMock(value="PT504884620", scheme_name="VAT")
        notice = _stub_notice(awards=[_stub_award(value=value)],
                              organizations={"O1": _org("Visualforma, S.A.",
                                                        country="PRT")})
        notice.buyer.return_value = buyer
        notice.cpv_main = "32420000"
        return notice

    def _lookups(self):
        return Lookups(peer_stats=InMemoryPeerStats({
            ("PRT", "3242"): PeerBand(n=412, p10=8e3, median=4.5e4,
                                      p90=1.8e5, p99=9e5)}))

    def test_an_outlier_is_withheld_with_its_reason(self):
        _, emit, _ = _run(self._notice(24474133.0), lookups=self._lookups())
        [contract] = _payloads(emit, "UpsertContract")
        assert contract["value_quarantine_reason"] == (
            "ambiguous_scale_x100_or_x1000")
        assert contract.get("value_eur") is None
        assert "generic.value_peer_outlier" in contract["cleaning_rules"]

    def test_a_plausible_value_is_stored(self):
        _, emit, _ = _run(self._notice(120000.0), lookups=self._lookups())
        [contract] = _payloads(emit, "UpsertContract")
        assert contract["value_eur"] == 120000.0
        assert contract.get("value_quarantine_reason") is None

    def test_without_peer_stats_nothing_is_quarantined(self):
        _, emit, _ = _run(self._notice(24474133.0))
        [contract] = _payloads(emit, "UpsertContract")
        assert contract.get("value_quarantine_reason") is None


class TestFrameworkGate:
    def _notice(self, framework_notice_id="536632-2024"):
        notice = _stub_notice(awards=[_stub_award()],
                              organizations={"O1": _org("Alfa S.p.A.")})
        notice.is_framework = True
        notice.framework_max_value = 5_000_000.0
        notice.framework_max_value_currency = "EUR"
        notice.framework_duration_months = 48
        notice.framework_max_operators = 5
        # eforms-parser 0.13 hands the loader the OPT-100 key already
        # normalised; 536632-2024 is the one notices 761784-2024 and
        # 3406-2025 both publish.
        notice.framework_notice_id = framework_notice_id
        notice.framework_notice_id_source = "opt-100"
        return notice

    def test_the_event_is_not_emitted_until_the_sink_understands_it(self):
        _, emit, _ = _run(self._notice(), emit_frameworks=False)
        types = [c.args[0] for c in emit.upsert.call_args_list]
        assert "UpsertFrameworkAgreement" not in types

    def test_the_terms_still_travel_on_the_contract(self):
        _, emit, _ = _run(self._notice(), emit_frameworks=False)
        [contract] = _payloads(emit, "UpsertContract")
        assert contract["framework_max_value_eur"] == 5_000_000.0
        assert contract["framework_duration_months"] == 48
        assert contract["framework_max_operators"] == 5

    def test_enabling_the_flag_emits_the_agreement(self):
        _, emit, _ = _run(self._notice(), emit_frameworks=True)
        types = [c.args[0] for c in emit.upsert.call_args_list]
        assert "UpsertFrameworkAgreement" in types


class TestFrameworkGroupingKey:
    """OPT-100 is what makes a framework's notices find each other, so
    the loader has to put the SAME value on the contract and on the
    agreement node - the old code keyed the node on contract_key (BT-04
    ContractFolderID), a value no contract could ever name."""

    def _notice(self, **over):
        notice = _stub_notice(awards=[_stub_award()],
                              organizations={"O1": _org("Alfa S.p.A.")})
        notice.is_framework = over.pop("is_framework", True)
        notice.framework_max_value = 5_000_000.0
        notice.framework_max_value_currency = "EUR"
        notice.framework_duration_months = 48
        notice.framework_max_operators = 5
        notice.framework_notice_id = over.pop("key", "536632-2024")
        notice.framework_notice_id_source = over.pop("source", "opt-100")
        return notice

    def test_the_key_rides_on_the_contract(self):
        _, emit, _ = _run(self._notice())
        [contract] = _payloads(emit, "UpsertContract")
        assert contract["framework_id"] == "536632-2024"

    def test_a_notice_with_a_key_but_no_framework_terms_still_carries_it(self):
        """Nothing gates the key on is_framework: a notice that names a
        framework it draws from belongs in that cluster whether or not
        it published the procedure's own terms."""
        _, emit, _ = _run(self._notice(is_framework=False))
        [contract] = _payloads(emit, "UpsertContract")
        assert contract["framework_id"] == "536632-2024"
        assert "framework_max_value_eur" not in contract

    def test_no_key_leaves_the_field_off_the_event(self):
        """Absence of a key is not absence of a framework - pre-2024
        notices have none at all - so it is simply not published."""
        _, emit, _ = _run(self._notice(key=None))
        [contract] = _payloads(emit, "UpsertContract")
        assert "framework_id" not in contract

    def test_the_agreement_is_keyed_on_the_same_value(self):
        _, emit, _ = _run(self._notice(), emit_frameworks=True)
        [agreement] = _payloads(emit, "UpsertFrameworkAgreement")
        assert agreement["framework_id"] == "536632-2024"
        [call] = [c for c in emit.upsert.call_args_list
                  if c.args[0] == "UpsertFrameworkAgreement"]
        assert call.kwargs["iri"].endswith("/FrameworkAgreement/536632-2024")

    def test_without_a_key_no_agreement_is_invented(self):
        """A node keyed on something no contract references is
        unreachable by construction; the terms still reach the
        contract."""
        _, emit, _ = _run(self._notice(key=None), emit_frameworks=True)
        types = [c.args[0] for c in emit.upsert.call_args_list]
        assert "UpsertFrameworkAgreement" not in types
        [contract] = _payloads(emit, "UpsertContract")
        assert contract["framework_max_value_eur"] == 5_000_000.0

    def test_the_provenance_travels_with_the_key(self):
        """opt-100 and the BT-125 fallback are not equally strong
        evidence, so the consumer has to be able to tell them apart."""
        _, emit, _ = _run(self._notice(source="bt-125"))
        [contract] = _payloads(emit, "UpsertContract")
        assert contract["framework_id_source"] == "bt-125"

    def test_the_flag_defaults_to_off(self, monkeypatch):
        monkeypatch.delenv(FRAMEWORKS_ENV, raising=False)
        assert frameworks_enabled() is False
        monkeypatch.setenv(FRAMEWORKS_ENV, "true")
        assert frameworks_enabled() is True
        monkeypatch.setenv(FRAMEWORKS_ENV, "no")
        assert frameworks_enabled() is False


class TestDryRun:
    def test_it_reports_without_writing_anything(self):
        notice = _stub_notice(awards=[_stub_award()],
                              organizations={"O1": _org(ITALIAN_JUNK)})
        log = DryRunEventLog()
        report = CleaningReport()
        with patch("src.etl.load_ted_contracts.TedRawStore") as raw_store:
            _run(notice, report=report, dry_run=True, log=log)
        raw_store.from_env.assert_not_called()
        assert log.total > 0                      # payloads were built...
        assert log.counts["UpsertContract"] == 1  # ...and validated
        out = report.as_dict()
        assert out["totals"]["notices"] == 1
        assert out["totals"]["suppliers_withheld"] == 1
        assert out["by_rule"]["it.notice_text_in_supplier_name"] == 1
        assert out["examples"]["it.notice_text_in_supplier_name"][0][
            "name_raw"] == ITALIAN_JUNK

    def test_the_report_is_filled_on_a_real_run_too(self):
        notice = _stub_notice(awards=[_stub_award()],
                              organizations={"O1": _org(ITALIAN_JUNK)})
        report = CleaningReport()
        _run(notice, report=report)
        assert report.as_dict()["totals"]["notices_with_rules"] == 1
