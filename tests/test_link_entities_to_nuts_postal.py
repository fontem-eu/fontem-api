"""Tests for link_entities_to_nuts_postal."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

from src.etl.link_entities_to_nuts_postal import (
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


# ── link_companies ───────────────────────────────────────────────────


def _make_session(company_rows, relationships_created=0):
    """Fake a paginating Neo4j session: the first fetch page returns
    ``company_rows``, the next returns empty (end of scan); MERGE queries
    return a summary carrying ``relationships_created``."""
    session = MagicMock()
    merge_summary = MagicMock()
    merge_summary.counters.relationships_created = relationships_created
    merge_result = MagicMock()
    merge_result.consume.return_value = merge_summary
    state = {"fetched": False}

    def _run(query, **_kwargs):
        if "MERGE" in query:
            return merge_result
        result = MagicMock()
        result.data.return_value = [] if state["fetched"] else company_rows
        state["fetched"] = True
        return result

    session.run.side_effect = _run
    return session


def _merge_calls(session):
    return [c for c in session.run.call_args_list if "MERGE" in c.args[0]]


def test_link_resolves_and_merges():
    lookup = {("DE", "80331"): "DE212"}
    session = _make_session(
        [{"gmr_id": "abc", "country": "DEU", "postal_code": "80331"}],
        relationships_created=1,
    )
    created = link_companies(session, lookup, batch_size=100)
    assert created == 1
    merge_call = _merge_calls(session)[0]
    batch = merge_call.kwargs["batch"]
    assert batch[0]["nuts3"] == "DE212"
    # The MERGE must key NUTSRegion on `code` (the real materialized property);
    # a stale `nuts_code` matches nothing and silently creates zero edges.
    merge_query = merge_call.args[0]
    assert "NUTSRegion {code:" in merge_query
    assert "nuts_code" not in merge_query


def test_link_skips_unresolvable():
    lookup = {}  # no mappings
    session = _make_session(
        [{"gmr_id": "xyz", "country": "DEU", "postal_code": "99999"}],
    )
    created = link_companies(session, lookup, batch_size=100)
    assert created == 0
    # Nothing resolved → no MERGE query should have run
    assert _merge_calls(session) == []


def test_link_normalises_postal_spaces():
    lookup = {("NL", "3204XD"): "NL366"}
    session = _make_session(
        [{"gmr_id": "nl1", "country": "NLD", "postal_code": "3204 XD"}],
        relationships_created=1,
    )
    created = link_companies(session, lookup, batch_size=100)
    assert created == 1


def test_link_handles_greece_country_code():
    # GRC (alpha-3) must map to the NUTS country code EL, not GR.
    lookup = {("EL", "10552"): "EL301"}
    session = _make_session(
        [{"gmr_id": "gr1", "country": "GRC", "postal_code": "10552"}],
        relationships_created=1,
    )
    created = link_companies(session, lookup, batch_size=100)
    assert created == 1
