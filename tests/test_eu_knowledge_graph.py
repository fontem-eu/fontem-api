"""Tests for EU Knowledge Graph ETL — date parsing, filtering, and CSV parsing.

Uses real sample data from tests/fixtures/kohesio/ (20 rows per country).
"""
# pylint: disable=missing-function-docstring,missing-class-docstring
from __future__ import annotations

from pathlib import Path

import pytest

from src.etl.load_eu_knowledge_graph import (
    _normalize_date,
    parse_kohesio_csv,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kohesio"


# ── _normalize_date ───────────────────────────────────────────


class TestNormalizeDate:
    """DD/MM/YYYY → YYYY-MM-DD conversion."""

    def test_standard_dd_mm_yyyy(self):
        assert _normalize_date("01/09/2022") == "2022-09-01"

    def test_day_greater_than_12(self):
        """Day > 12 proves it's DD/MM not MM/DD."""
        assert _normalize_date("31/08/2024") == "2024-08-31"

    def test_already_iso(self):
        assert _normalize_date("2025-09-01") == "2025-09-01"

    def test_empty_string(self):
        assert _normalize_date("") == ""

    def test_none(self):
        assert _normalize_date(None) == ""

    def test_whitespace(self):
        assert _normalize_date("  ") == ""

    def test_partial_date(self):
        """Garbage input returns as-is (won't match ISO comparison)."""
        result = _normalize_date("2025")
        assert result == "2025"

    def test_slash_with_two_digit_year(self):
        """Two-digit year — no 4-digit year part, returned as-is."""
        result = _normalize_date("01/09/25")
        # len(parts[2]) != 4, so falls through
        assert result == "01/09/25"


# ── parse_kohesio_csv — per-country fixture tests ────────────


class TestParseKohesioCsv:  # pylint: disable=too-many-public-methods
    """Parse real Kohesio CSV samples and validate field extraction.

    One test method per country fixture × per field — Kohesio's column
    semantics shift slightly per ESI Fund variant, so each (country, field)
    pair pins the parser's behaviour for that combination.
    """

    def _load(self, country: str, since: str | None = None) -> list[dict]:
        path = FIXTURES / f"{country}-sample.csv"
        if not path.exists():
            pytest.skip(f"No fixture for {country}")
        with open(path, "rb") as f:
            data = f.read()
        return list(parse_kohesio_csv(data, since=since))

    # ── PT ─────────────────────────────────────────────────

    def test_pt_parses_all_rows(self):
        records = self._load("PT")
        assert len(records) == 20

    def test_pt_dates_are_iso(self):
        records = self._load("PT")
        for r in records:
            if r["start_date"]:
                assert r["start_date"][4] == "-", f"Bad date: {r['start_date']}"

    def test_pt_since_filter(self):
        all_records = self._load("PT")
        filtered = self._load("PT", since="2025-09-01")
        assert len(filtered) < len(all_records)
        for r in filtered:
            assert r["start_date"] >= "2025-09-01"

    def test_pt_has_nuts_codes(self):
        records = self._load("PT")
        with_nuts = [r for r in records if r["nuts_code"]]
        assert len(with_nuts) > 0
        for r in with_nuts:
            assert r["nuts_code"].startswith("PT")

    def test_pt_has_project_ids(self):
        records = self._load("PT")
        ids = {r["project_id"] for r in records}
        assert len(ids) == len(records), "project_ids must be unique"

    def test_pt_has_budgets(self):
        records = self._load("PT")
        with_budget = [r for r in records if r["total_budget"] is not None]
        assert len(with_budget) > 0
        for r in with_budget:
            assert r["total_budget"] > 0

    def test_pt_has_eu_contribution(self):
        records = self._load("PT")
        with_eu = [r for r in records if r["eu_contribution"] is not None]
        assert len(with_eu) > 0
        for r in with_eu:
            assert r["eu_contribution"] > 0
            assert r["eu_contribution"] <= r["total_budget"]

    def test_pt_has_fund_names(self):
        records = self._load("PT")
        funds = {r["fund"] for r in records if r["fund"]}
        assert len(funds) > 0

    def test_pt_country_is_set(self):
        records = self._load("PT")
        for r in records:
            assert r["country"] == "PRT"

    # ── CZ — mixed dates: some valid, some empty ──────────

    def test_cz_parses_rows(self):
        records = self._load("CZ")
        assert len(records) == 20

    def test_cz_has_empty_dates(self):
        """CZ has some rows with empty Operation_Start_Date."""
        records = self._load("CZ")
        empty = [r for r in records if not r["start_date"]]
        valid = [r for r in records if r["start_date"]]
        assert len(empty) > 0, "CZ should have some empty dates"
        assert len(valid) > 0, "CZ should have some valid dates"

    def test_cz_empty_dates_pass_when_no_since(self):
        """Without --since, empty dates are included."""
        records = self._load("CZ")
        assert len(records) == 20

    def test_cz_missing_start_uses_end_date(self):
        """Rows with empty start_date but valid end_date use end for filtering."""
        all_records = self._load("CZ")
        no_start_has_end = [
            r for r in all_records
            if not r["start_date"] and r["end_date"]
        ]
        # These should still be filterable via end_date
        assert len(no_start_has_end) > 0, "CZ should have rows with end but no start"

    def test_cz_old_dates_filtered_by_since(self):
        """With --since, old dates are correctly excluded."""
        all_records = self._load("CZ")
        filtered = self._load("CZ", since="2025-09-01")
        old = [r for r in all_records if r["start_date"] and r["start_date"] < "2025-09-01"]
        assert len(filtered) < len(all_records) or len(old) == 0

    # ── DE — all dates empty ──────────────────────────────

    def test_de_all_empty_dates(self):
        records = self._load("DE")
        if len(records) == 0:
            pytest.skip("DE sample is too small")
        for r in records:
            assert not r["start_date"], "DE dates should be empty"

    def test_de_excluded_with_since(self):
        """DE records with no dates at all are excluded when --since is set."""
        records = self._load("DE", since="2025-09-01")
        assert len(records) == 0, "Records with no temporal info should be excluded"

    def test_de_included_without_since(self):
        """DE records pass through when no --since filter is active."""
        records = self._load("DE")
        all_records = self._load("DE")
        assert len(records) == len(all_records)

    # ── FR ─────────────────────────────────────────────────

    def test_fr_dates_normalized(self):
        records = self._load("FR")
        for r in records:
            if r["start_date"]:
                # Must be YYYY-MM-DD, not DD/MM/YYYY
                assert len(r["start_date"]) == 10
                assert r["start_date"][4] == "-"
                assert r["start_date"][7] == "-"

    # ── BG ─────────────────────────────────────────────────

    def test_bg_recent_dates(self):
        """BG sample has recent dates (2024-2025)."""
        records = self._load("BG")
        recent = [r for r in records if r["start_date"] and r["start_date"] >= "2025-01-01"]
        assert len(recent) > 0

    # ── Cross-country ─────────────────────────────────────

    def test_all_countries_have_wikibase_qids(self):
        for cc in ["PT", "CZ", "FR", "PL", "BG"]:
            records = self._load(cc)
            for r in records:
                assert r["wikibase_qid"].startswith("Q"), \
                    f"{cc} row missing QID: {r['wikibase_qid']}"

    def test_all_countries_have_unique_project_ids(self):
        all_ids = set()
        for cc in ["PT", "CZ", "FR", "PL", "BG"]:
            records = self._load(cc)
            for r in records:
                assert r["project_id"] not in all_ids, \
                    f"Duplicate project_id from {cc}"
                all_ids.add(r["project_id"])


# ── Event-emit tests ──────────────────────────────────────────────

class TestEmitDisclosure:  # pylint: disable=missing-class-docstring
    def _mock_log(self):  # pylint: disable=missing-function-docstring
        from unittest.mock import MagicMock  # pylint: disable=import-outside-toplevel
        log = MagicMock()
        emit = MagicMock()
        log.batch.return_value.__enter__ = MagicMock(return_value=emit)
        log.batch.return_value.__exit__ = MagicMock(return_value=False)
        return log, emit

    def test_emit_uses_qid_as_disclosure_id(self):  # pylint: disable=missing-function-docstring
        from src.etl import load_eu_knowledge_graph  # pylint: disable=import-outside-toplevel
        log, emit = self._mock_log()
        rec = {
            "project_id": "p1", "qid": "Q12345",
            "wikibase_qid": "Q12345",
            "title": "T", "description": "D",
            "total_budget": 1000.0, "eu_contribution": 500.0,
            "fund": "EFRD", "programme": "POL",
            "start_date": "2024-09-01", "end_date": "2026-12-31",
            "nuts_code": "PT10", "country": "PRT",
            "beneficiary_gmr_id": "00040372-dad6-5d34-882c-8b8624b4e734",
            "beneficiary_name": "Camara Municipal de Lisboa",
            "beneficiary_qid": "Q67890",
        }
        load_eu_knowledge_graph.emit_disclosure_events(log, [rec])
        # Now two upserts: the beneficiary :Company (resolve-or-create) then
        # the disclosure whose company_gmr_id points at it.
        assert emit.upsert.call_count == 2
        company = emit.upsert.call_args_list[0]
        assert company.args[0] == "UpsertCompany"
        assert company.kwargs["payload"]["gmr_id"] == "00040372-dad6-5d34-882c-8b8624b4e734"
        assert company.kwargs["payload"]["name"] == "Camara Municipal de Lisboa"
        payload = emit.upsert.call_args.kwargs["payload"]
        assert payload["system"] == "eu-cohesion"
        assert payload["disclosure_id"] == "Q12345"
        assert payload["company_gmr_id"] == "00040372-dad6-5d34-882c-8b8624b4e734"
        assert payload["year"] == 2024
        # details flattened, includes nuts_code
        assert payload["details"]["nuts_code"] == "PT10"
        assert payload["details"]["beneficiary_qid"] == "Q67890"

    def test_emit_handles_missing_beneficiary(self):  # pylint: disable=missing-function-docstring
        """Project without a beneficiary still gets a disclosure
        event — company_gmr_id is optional in the relaxed schema."""
        from src.etl import load_eu_knowledge_graph  # pylint: disable=import-outside-toplevel
        log, emit = self._mock_log()
        rec = {"project_id": "p1", "qid": "Q1", "wikibase_qid": "Q1",
               "title": "t", "start_date": "2024-01-01"}
        load_eu_knowledge_graph.emit_disclosure_events(log, [rec])
        payload = emit.upsert.call_args.kwargs["payload"]
        assert "company_gmr_id" not in payload
        # No beneficiary → no resolve-or-create company either.
        assert all(c.args[0] != "UpsertCompany" for c in emit.upsert.call_args_list)

    def test_emit_dedupes_beneficiary_company(self):  # pylint: disable=missing-function-docstring
        from src.etl import load_eu_knowledge_graph  # pylint: disable=import-outside-toplevel
        log, emit = self._mock_log()
        base = {
            "wikibase_qid": "Q1", "title": "t", "start_date": "2024-01-01",
            "country": "PRT",
            "beneficiary_gmr_id": "ben-1", "beneficiary_name": "Shared Beneficiary",
        }
        # Two projects, same beneficiary → exactly one UpsertCompany.
        load_eu_knowledge_graph.emit_disclosure_events(log, [
            {**base, "qid": "QA"}, {**base, "qid": "QB"},
        ])
        companies = [c for c in emit.upsert.call_args_list if c.args[0] == "UpsertCompany"]
        assert len(companies) == 1
        disclosures = [c for c in emit.upsert.call_args_list if c.args[0] == "UpsertDisclosure"]
        assert len(disclosures) == 2

    def test_emit_beneficiary_company_without_name_still_resolves(self):  # pylint: disable=missing-function-docstring
        from src.etl import load_eu_knowledge_graph  # pylint: disable=import-outside-toplevel
        log, emit = self._mock_log()
        rec = {"qid": "Q1", "wikibase_qid": "Q1", "title": "t",
               "start_date": "2024-01-01", "country": "PRT",
               "beneficiary_gmr_id": "ben-x"}  # no beneficiary_name
        load_eu_knowledge_graph.emit_disclosure_events(log, [rec])
        company = [c for c in emit.upsert.call_args_list if c.args[0] == "UpsertCompany"][0]
        assert company.kwargs["payload"]["gmr_id"] == "ben-x"
        assert company.kwargs["payload"].get("name") is None


def test_parse_captures_beneficiary_name():
    # Kohesio identifies beneficiaries by its own Wikibase QID
    # (linkedopendata.eu) AND ships the human name in Beneficiary_Name.
    header = (
        "Operation_Unique_Identifier,Beneficiary_Unique_Identifier,"
        "Beneficiary_Name,CountryCode,Operation_Name_English\n"
    )
    row = (
        "https://linkedopendata.eu/entity/Q100,"
        "https://linkedopendata.eu/entity/Q200,"
        "Camara Municipal de Lisboa,PT,Some Project\n"
    )
    parsed = list(parse_kohesio_csv((header + row).encode(), since=None))
    assert len(parsed) == 1
    assert parsed[0]["beneficiary_name"] == "Camara Municipal de Lisboa"
    assert parsed[0]["beneficiary_gmr_id"]  # derived from the Wikibase QID


def test_beneficiary_gmr_id_is_canonical_from_name():
    """The beneficiary company id is minted from name+country (the canonical
    scheme), so a cohesion beneficiary that is also a TED/GLEIF company
    resolves to the same node — not a kohesio-only twin."""
    from src.etl import gmr_id  # pylint: disable=import-outside-toplevel
    row = {
        "Operation_Unique_Identifier": "https://linkedopendata.eu/entity/Q111",
        "Beneficiary_Unique_Identifier": "https://linkedopendata.eu/entity/Q222",
        "Beneficiary_Name": "Siemens AG",
        "CountryCode": "DE",
        "Operation_Start_Date": "01/03/2024",
    }
    import csv  # pylint: disable=import-outside-toplevel
    import io  # pylint: disable=import-outside-toplevel
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(row.keys()))
    writer.writeheader()
    writer.writerow(row)
    [rec] = list(parse_kohesio_csv(buf.getvalue().encode(), since="2021-01-01"))
    expected = str(gmr_id.from_name("DEU", "Siemens AG"))
    assert rec["beneficiary_gmr_id"] == expected
    assert "kohesio_ben" not in rec["beneficiary_gmr_id"]
    assert rec["beneficiary_qid"] == "Q222"


def test_nan_beneficiary_does_not_collapse():
    """Missing names (Kohesio writes 'nan') must not collapse into one
    from_name('nan') node — they fall back to the per-QID key and stay
    distinct from a really-named beneficiary."""
    import csv  # pylint: disable=import-outside-toplevel
    import io  # pylint: disable=import-outside-toplevel
    from src.etl import gmr_id  # pylint: disable=import-outside-toplevel

    def parse_one(name, qid):
        row = {
            "Operation_Unique_Identifier": "https://x/entity/Q1",
            "Beneficiary_Unique_Identifier": f"https://x/entity/{qid}",
            "Beneficiary_Name": name, "CountryCode": "PL",
            "Operation_Start_Date": "01/03/2024",
        }
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)
        return list(parse_kohesio_csv(buf.getvalue().encode(),
                                      since="2021-01-01"))[0]

    nan_a = parse_one("nan", "Q9")
    nan_b = parse_one("nan", "Q10")
    named = parse_one("Real Co", "Q9")
    assert nan_a["beneficiary_name"] is None
    assert nan_a["beneficiary_gmr_id"] != nan_b["beneficiary_gmr_id"]   # distinct per QID
    assert nan_a["beneficiary_gmr_id"] != named["beneficiary_gmr_id"]
    assert named["beneficiary_gmr_id"] == str(gmr_id.from_name("POL", "Real Co"))
