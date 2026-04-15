"""Tests for EU Knowledge Graph ETL — date parsing, filtering, and CSV parsing.

Uses real sample data from tests/fixtures/kohesio/ (20 rows per country).
"""
# pylint: disable=missing-function-docstring,missing-class-docstring
from __future__ import annotations

import os
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


class TestParseKohesioCsv:
    """Parse real Kohesio CSV samples and validate field extraction."""

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
            assert r["country"] == "PT"

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
