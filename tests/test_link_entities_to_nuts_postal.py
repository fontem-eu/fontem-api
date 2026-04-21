"""Tests for link_entities_to_nuts_postal."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

from src.etl.link_entities_to_nuts_postal import (
    _nuts_country,
    link_companies,
    load_postal_lookup,
)


def _make_postal_zip(csv_content: str) -> io.BytesIO:
    """Build an in-memory zip containing the postal CSV."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PCODE_2025_NUTS-2024_v2.0.csv", csv_content)
    buf.seek(0)
    return buf


# ── load_postal_lookup ───────────────────────────────────────────────


def test_load_parses_rows():
    csv = "NUTS3;CODE\n'DE212';'80331'\n'NL366';'3204 XD'\n"
    buf = _make_postal_zip(csv)
    lookup = load_postal_lookup(buf)
    assert lookup[("DE", "80331")] == "DE212"
    # spaces are stripped from the postal code key
    assert lookup[("NL", "3204XD")] == "NL366"


def test_load_strips_single_quotes():
    csv = "NUTS3;CODE\n'FR101';'75001'\n"
    buf = _make_postal_zip(csv)
    lookup = load_postal_lookup(buf)
    assert ("FR", "75001") in lookup


def test_load_skips_short_nuts():
    csv = "NUTS3;CODE\n'X';'12345'\n'DE212';'80331'\n"
    buf = _make_postal_zip(csv)
    lookup = load_postal_lookup(buf)
    assert len(lookup) == 1


def test_load_handles_bom():
    csv = "\ufeffNUTS3;CODE\n'AT130';'1010'\n"
    buf = _make_postal_zip(csv)
    lookup = load_postal_lookup(buf)
    assert lookup[("AT", "1010")] == "AT130"


# ── _nuts_country ────────────────────────────────────────────────────


def test_nuts_country_passthrough():
    assert _nuts_country("DE") == "DE"
    assert _nuts_country("FR") == "FR"


def test_nuts_country_greece_mapped():
    assert _nuts_country("GR") == "EL"


def test_nuts_country_case_insensitive():
    assert _nuts_country("gr") == "EL"
    assert _nuts_country("de") == "DE"


# ── link_companies ───────────────────────────────────────────────────


def _make_session(company_rows, relationships_created=0):
    session = MagicMock()
    fetch_result = MagicMock()
    fetch_result.data.return_value = company_rows
    merge_summary = MagicMock()
    merge_summary.counters.relationships_created = relationships_created
    merge_result = MagicMock()
    merge_result.consume.return_value = merge_summary
    # First run() call = fetch, subsequent = merge
    session.run.side_effect = [fetch_result, merge_result]
    return session


def test_link_resolves_and_merges():
    lookup = {("DE", "80331"): "DE212"}
    session = _make_session(
        [{"gmr_id": "abc", "country": "DE", "postal_code": "80331"}],
        relationships_created=1,
    )
    created = link_companies(session, lookup, batch_size=100)
    assert created == 1
    # Second run call should be the MERGE with correct data
    merge_call = session.run.call_args_list[1]
    batch = merge_call.kwargs["batch"] if "batch" in merge_call.kwargs else merge_call.args[1]
    assert batch[0]["nuts3"] == "DE212"


def test_link_skips_unresolvable():
    lookup = {}  # no mappings
    session = _make_session(
        [{"gmr_id": "xyz", "country": "DE", "postal_code": "99999"}],
    )
    created = link_companies(session, lookup, batch_size=100)
    assert created == 0
    # Only the fetch query should have been called — no merge
    assert session.run.call_count == 1


def test_link_normalises_postal_spaces():
    lookup = {("NL", "3204XD"): "NL366"}
    session = _make_session(
        [{"gmr_id": "nl1", "country": "NL", "postal_code": "3204 XD"}],
        relationships_created=1,
    )
    created = link_companies(session, lookup, batch_size=100)
    assert created == 1


def test_link_handles_greece_country_code():
    lookup = {("EL", "10552"): "EL301"}
    session = _make_session(
        [{"gmr_id": "gr1", "country": "GR", "postal_code": "10552"}],
        relationships_created=1,
    )
    created = link_companies(session, lookup, batch_size=100)
    assert created == 1
