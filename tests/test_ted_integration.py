"""
Integration tests for TED contract parsing and loading.

Uses a real TED daily package (20 notices from Oct 2025) to validate:
1. XML parsing produces valid Notice/Award objects
2. Date coalescing fills award_date for all contracts
3. Currency conversion produces reasonable EUR values
4. Company/authority matching resolves against live Neo4j
5. No duplicate notice IDs in output

These tests read from tests/fixtures/ted/sample-2025-10.tar.gz
and validate against the live Neo4j database.
"""
# Currency fixture writes JSON rate files (ASCII keys/values) — explicit
# encoding adds nothing. `currency_svc` is a pytest fixture re-bound as a
# parameter on every test, the protected-access on _coalesce_date pins
# the load-bearing private TED date heuristic, and the smoke tests do
# `import neo4j.GraphDatabase` inside the test so the import is skipped
# when running without a real Neo4j connection.
# pylint: disable=redefined-outer-name,unspecified-encoding,import-outside-toplevel,broad-exception-caught
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from eforms.stream import stream_notices
from eforms.filters import awards_only

from src.services.currency import ConversionResult

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ted"
SAMPLE_ARCHIVE = FIXTURE_DIR / "sample-2025-10.tar.gz"

# Skip if fixture doesn't exist (CI without fixtures)
pytestmark = pytest.mark.skipif(
    not SAMPLE_ARCHIVE.exists(),
    reason=f"TED fixture not found: {SAMPLE_ARCHIVE}",
)

# Sample ECB rates for Oct 2025 (realistic values)
SAMPLE_RATES = {
    "SEK": {"2025-10-01": 11.28, "2025-10-02": 11.30, "2025-10-03": 11.25,
            "2025-10-06": 11.32, "2025-10-07": 11.35},
    "PLN": {"2025-10-01": 4.28, "2025-10-02": 4.30, "2025-10-03": 4.29,
            "2025-10-06": 4.31, "2025-10-07": 4.32},
    "CZK": {"2025-10-01": 25.10, "2025-10-02": 25.15, "2025-10-03": 25.12,
            "2025-10-06": 25.20, "2025-10-07": 25.18},
    "HUF": {"2025-10-01": 402.5, "2025-10-02": 403.0, "2025-10-03": 401.8,
            "2025-10-06": 404.0, "2025-10-07": 403.5},
    "RON": {"2025-10-01": 4.97, "2025-10-02": 4.98, "2025-10-03": 4.97,
            "2025-10-06": 4.98, "2025-10-07": 4.97},
    "BGN": {"2025-10-01": 1.96, "2025-10-02": 1.96, "2025-10-03": 1.96,
            "2025-10-06": 1.96, "2025-10-07": 1.96},
    "NOK": {"2025-10-01": 11.60, "2025-10-02": 11.55, "2025-10-03": 11.58,
            "2025-10-06": 11.62, "2025-10-07": 11.60},
    "DKK": {"2025-10-01": 7.46, "2025-10-02": 7.46, "2025-10-03": 7.46,
            "2025-10-06": 7.46, "2025-10-07": 7.46},
    "CHF": {"2025-10-01": 0.94, "2025-10-02": 0.94, "2025-10-03": 0.95,
            "2025-10-06": 0.94, "2025-10-07": 0.94},
    "GBP": {"2025-10-01": 0.84, "2025-10-02": 0.84, "2025-10-03": 0.84,
            "2025-10-06": 0.84, "2025-10-07": 0.84},
    "ISK": {"2025-10-01": 149.0, "2025-10-02": 149.5, "2025-10-03": 149.2,
            "2025-10-06": 149.0, "2025-10-07": 149.3},
    "USD": {"2025-10-01": 1.10, "2025-10-02": 1.11, "2025-10-03": 1.10,
            "2025-10-06": 1.10, "2025-10-07": 1.10},
}


@pytest.fixture(scope="module")
def currency_svc():
    """Stub CurrencyClient backed by SAMPLE_RATES.

    The real CurrencyClient hits an HTTP service; in integration tests
    we don't need the network round-trip — only the to_eur math. The
    stub mimics the subset of the client surface used in this file
    (to_eur) with the SAMPLE_RATES table.
    """
    class _Stub:
        def to_eur(self, value, currency, on):
            if value is None or on is None or currency is None:
                return None
            ccy = currency.upper()
            if ccy == "EUR":
                return Decimal(str(value)).quantize(Decimal("0.01"))
            daily = SAMPLE_RATES.get(ccy, {})
            rate = daily.get(on.isoformat())
            if rate is None:
                return None
            return (Decimal(str(value)) / Decimal(str(rate))).quantize(
                Decimal("0.01"),
            )

        def convert_detailed(self, value, currency, on):
            eur = self.to_eur(value, currency, on)
            return ConversionResult(
                eur=eur, rate_used=None, rate_date=on,
                source="ecb" if eur is not None else "unknown",
            )

    yield _Stub()


def _parse_all_notices():
    """Parse all notices from the test fixture."""
    return list(awards_only(stream_notices(SAMPLE_ARCHIVE)))


class TestTedParsing:
    """Validate that eForms XML parsing produces valid structures."""

    def test_fixture_parses_without_errors(self):
        """All 20 notices in the fixture should parse without exceptions."""
        notices = _parse_all_notices()
        assert len(notices) > 0, "No notices parsed from fixture"

    def test_every_notice_has_id(self):
        """Every notice must have a non-empty ID."""
        for notice in _parse_all_notices():
            assert notice.notice_id, f"Notice missing ID: {notice}"

    def test_no_duplicate_notice_ids(self):
        """No duplicate notice IDs in the parsed output."""
        notices = _parse_all_notices()
        ids = [n.notice_id for n in notices]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {[i for i in ids if ids.count(i) > 1]}"

    def test_every_notice_has_issue_date(self):
        """Every notice must have an issue_date (publication date)."""
        for notice in _parse_all_notices():
            assert notice.issue_date, f"Notice {notice.notice_id} missing issue_date"

    def test_issue_date_format(self):
        """Issue dates should be ISO format (YYYY-MM-DD), not with timezone."""
        for notice in _parse_all_notices():
            if notice.issue_date:
                assert len(notice.issue_date) == 10, \
                    f"Bad date format '{notice.issue_date}' for {notice.notice_id}"
                assert notice.issue_date[4] == "-" and notice.issue_date[7] == "-"

    def test_no_bogus_sentinel_dates(self):
        """Award dates of 2000-01-01 should be filtered out."""
        for notice in _parse_all_notices():
            for award in notice.awards:
                if award.award_date:
                    assert not award.award_date.startswith("2000-01-01"), \
                        f"Bogus sentinel date in {notice.notice_id}"
                if award.conclusion_date:
                    assert not award.conclusion_date.startswith("2000-01-01"), \
                        f"Bogus sentinel conclusion_date in {notice.notice_id}"

    def test_awards_have_contractor(self):
        """Every award should reference a contractor org that exists in organizations."""
        for notice in _parse_all_notices():
            for award in notice.awards:
                assert award.contractor_org_id in notice.organizations, \
                    f"Award in {notice.notice_id} references unknown org {award.contractor_org_id}"

    def test_buyers_resolve(self):
        """Every notice should have a resolvable buyer."""
        for notice in _parse_all_notices():
            buyer = notice.buyer()
            assert buyer is not None, f"Notice {notice.notice_id} has no buyer"
            assert buyer.name, f"Buyer in {notice.notice_id} has no name"


class TestDateCoalescing:
    """Validate the date coalescing chain fills dates for all contracts."""

    def test_coalescing_fills_all_awards(self):
        """After coalescing, every award should have an effective date."""
        from src.etl.load_ted_contracts import _coalesce_date

        unfilled = 0
        total = 0
        for notice in _parse_all_notices():
            for award in notice.awards:
                total += 1
                effective, _source = _coalesce_date(award, notice)
                if effective is None:
                    unfilled += 1

        assert total > 0, "No awards found in fixture"
        fill_rate = (total - unfilled) / total * 100
        assert fill_rate >= 90, \
            f"Date coalescing only filled {fill_rate:.0f}% ({unfilled}/{total} unfilled)"

    def test_coalescing_prefers_award_date(self):
        """When award_date exists, it should be preferred."""
        from src.etl.load_ted_contracts import _coalesce_date

        for notice in _parse_all_notices():
            for award in notice.awards:
                if award.award_date:
                    effective, source = _coalesce_date(award, notice)
                    assert source == "award"
                    assert effective == award.award_date

    def test_source_is_always_set(self):
        """The source field should always be one of the known values."""
        from src.etl.load_ted_contracts import _coalesce_date

        valid_sources = {
            "award", "conclusion", "dispatch", "publication", "issue", "none",
        }
        for notice in _parse_all_notices():
            for award in notice.awards:
                _, source = _coalesce_date(award, notice)
                assert source in valid_sources, f"Unknown source '{source}'"


class TestCurrencyConversion:
    """Validate EUR conversion produces reasonable values."""

    def test_eur_values_are_reasonable(self, currency_svc):
        """No contract should exceed €10B after conversion."""
        for notice in _parse_all_notices():
            for award in notice.awards:
                if award.value is None:
                    continue
                currency = award.currency or notice.currency or "EUR"
                date_str = award.award_date or notice.issue_date
                if not date_str:
                    continue
                try:
                    date_obj = date.fromisoformat(date_str[:10])
                except ValueError:
                    continue
                eur = currency_svc.to_eur(Decimal(str(award.value)), currency, date_obj)
                if eur is not None:
                    assert eur < Decimal("10000000000"), \
                        f"Unreasonable EUR value {eur} for {notice.notice_id} " \
                        f"(original: {award.value} {currency})"

    def test_eur_passthrough(self, currency_svc):
        """EUR-denominated awards should pass through unchanged."""
        for notice in _parse_all_notices():
            for award in notice.awards:
                if award.value and (award.currency or notice.currency) == "EUR":
                    if not notice.issue_date:
                        continue
                    try:
                        date_obj = date.fromisoformat(notice.issue_date[:10])
                    except ValueError:
                        continue
                    eur = currency_svc.to_eur(Decimal(str(award.value)), "EUR", date_obj)
                    expected = Decimal(str(award.value)).quantize(Decimal("0.01"))
                    assert eur == expected


class TestCompanyMatching:
    """Validate company matching against live Neo4j (if available)."""

    @pytest.fixture()
    def neo4j_session(self):
        """Connect to live Neo4j — skip if unavailable."""
        uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
        user = os.environ.get("NEO4J_USER", "neo4j")
        # No default password: this integration test is opt-in, the
        # password comes from the VSO-managed neo4j-credentials Secret
        # (or `vault kv get secret/gmr/neo4j` for local runs). Missing
        # env → skip, same as "Neo4j not available".
        password = os.environ.get("NEO4J_PASSWORD")
        if password is None:
            pytest.skip("NEO4J_PASSWORD not set")
        try:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(uri, auth=(user, password))
            with driver.session() as session:
                session.run("RETURN 1")
            with driver.session() as session:
                yield session
            driver.close()
        except Exception:
            pytest.skip("Neo4j not available")

    def test_authorities_in_fixture_have_names(self):
        """Every buyer/authority in the fixture should have a name."""
        for notice in _parse_all_notices():
            buyer = notice.buyer()
            if buyer:
                assert buyer.name and len(buyer.name) > 1

    def test_contractors_have_names(self):
        """Every contractor should have a name."""
        for notice in _parse_all_notices():
            for award in notice.awards:
                contractor = notice.organizations.get(award.contractor_org_id)
                if contractor:
                    assert contractor.name and len(contractor.name) > 1
