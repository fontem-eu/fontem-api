"""Tests for the postal-code → NUTS-3 lookup helper."""
from __future__ import annotations

import zipfile

import pytest

from src.etl._pcode import VENDORED_PCODE_ZIP, load_lookup, normalise


# ── normalise() ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3204 XD", "3204XD"),
        ("3660-322", "3660322"),
        ("569 55", "56955"),
        ("f12", "F12"),
        ("  90210  ", "90210"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_strips_and_uppercases(raw, expected):
    assert normalise(raw) == expected


# ── load_lookup() — synthetic zip ─────────────────────────────────────


def _build_zip(tmp_path, csv_body: str, csv_name: str = "PCODE.csv"):
    zip_path = tmp_path / "pcode.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(csv_name, csv_body)
    return zip_path


def test_load_lookup_parses_quoted_semicolon_format(tmp_path):
    body = (
        "﻿NUTS3;CODE\n"
        "'NL366';'3204 XD'\n"
        "'NL366';'3204 XT'\n"
        "'DEF09';'25495'\n"
    )
    lookup = load_lookup(_build_zip(tmp_path, body))
    assert lookup[("NL", "3204XD")] == "NL366"
    assert lookup[("NL", "3204XT")] == "NL366"
    assert lookup[("DE", "25495")] == "DEF09"


def test_load_lookup_first_write_wins_on_duplicate_postcode(tmp_path):
    """Same postcode appearing under two NUTS3 codes keeps the first one."""
    body = (
        "﻿NUTS3;CODE\n"
        "'PT194';'3660-322'\n"
        "'PT195';'3660-322'\n"
    )
    lookup = load_lookup(_build_zip(tmp_path, body))
    assert lookup[("PT", "3660322")] == "PT194"


def test_load_lookup_skips_malformed_rows(tmp_path):
    body = (
        "﻿NUTS3;CODE\n"
        "'NL366';'3204 XD'\n"
        "single-column-row\n"
        "';'';\n"  # empty NUTS3 + empty postcode
        "'NL366';''\n"  # empty postcode
        "'DE';'25495'\n"  # NUTS3 too short (< 3 chars) — skipped
    )
    lookup = load_lookup(_build_zip(tmp_path, body))
    assert ("NL", "3204XD") in lookup
    assert len(lookup) == 1


def test_load_lookup_raises_on_empty_csv(tmp_path):
    body = ""  # no header even
    with pytest.raises(ValueError):
        load_lookup(_build_zip(tmp_path, body))


# ── load_lookup() — real vendored zip ─────────────────────────────────


def test_vendored_zip_ships_with_repo():
    """The vendored PCODE zip must be present — the linker depends on it."""
    assert VENDORED_PCODE_ZIP.is_file(), (
        f"expected vendored PCODE zip at {VENDORED_PCODE_ZIP}"
    )


def test_load_lookup_default_path_returns_real_data():
    """Smoke-test the vendored zip: lookup is non-trivial, well-typed."""
    lookup = load_lookup()
    assert len(lookup) > 100_000
    # Every key must be (alpha-2 country, normalised postcode); every value
    # a NUTS-3 code starting with that country.
    for (country, _pc), nuts3 in list(lookup.items())[:1000]:
        assert len(country) == 2
        assert country.isupper()
        assert nuts3.startswith(country)
