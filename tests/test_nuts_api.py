"""The public NUTS reference API — the contract other people's code depends on.

These assert the published shape, not internals: field names, filters, paging,
the search ranking, provenance, and the cache/CORS headers. A change that
breaks one of these breaks somebody's client.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring

import csv
import io

import pytest
from fastapi.testclient import TestClient

from src.nuts_api.app import build_app


@pytest.fixture(name="client", scope="module")
def _client() -> TestClient:
    return TestClient(build_app())


# ── GET /nuts ──────────────────────────────────────────────────


def test_service_info_states_vintage_languages_and_coverage(client):
    body = client.get("/nuts").json()
    assert body["nuts_version"] == "2024"
    assert len(body["languages"]) == 24
    assert body["coverage"]["regions"] == 1798
    # Coverage is uneven and the API says so rather than implying completeness.
    assert 0 < body["coverage"]["with_translations"] < body["coverage"]["regions"]
    assert body["coverage"]["per_language"]["en"] > 1000
    assert set(body["endpoints"]) >= {"list", "one", "search", "bulk_json", "bulk_csv"}


def test_service_info_names_every_source_with_its_licence(client):
    """Republishing other people's data without saying whose it is, or under
    what terms, is not something to leave to a README nobody fetches."""
    sources = {s["id"]: s for s in client.get("/nuts").json()["sources"]}
    assert set(sources) == {"eurostat", "euvoc", "wikidata"}
    for source in sources.values():
        assert source["licence"] and source["attribution"] and source["url"]
    assert "CC0" in sources["wikidata"]["licence"]
    # Names are served, geometry is not — the distinction the terms turn on.
    assert "no boundary geometry" in client.get("/nuts").json()["licence"]


# ── GET /nuts/regions ──────────────────────────────────────────


def test_regions_carry_both_eurostat_forms_and_every_known_language(client):
    rows = {r["code"]: r for r in
            client.get("/nuts/regions?country=EL&level=1").json()["regions"]}
    el3 = rows["EL3"]
    assert (el3["name_native"], el3["name_latn"]) == ("Αττική", "Attiki")
    assert el3["names"]["en"] == "Attica Region"
    assert el3["names"]["el"] == "Περιφέρεια Αττικής"
    assert el3["parent"] == "EL" and el3["country"] == "EL"
    assert "wikidata" in el3["sources"]


def test_regions_filter_and_page_stably(client):
    everything = client.get("/nuts/regions?limit=2000").json()
    assert everything["total"] == 1798
    codes = [r["code"] for r in everything["regions"]]
    assert codes == sorted(codes)
    first = client.get("/nuts/regions?limit=5").json()
    second = client.get("/nuts/regions?limit=5&offset=5").json()
    assert [r["code"] for r in first["regions"]] == codes[:5]
    assert [r["code"] for r in second["regions"]] == codes[5:10]
    assert first["total"] == second["total"] == 1798


def test_regions_name_rows_in_the_requested_language(client):
    """The app needs one language per row, not a map of 24 — this is what
    replaced the separate /geo endpoint that used to do the projection."""
    rows = {r["code"]: r for r in
            client.get("/nuts/regions?codes=EL3,PT1A0&lang=el").json()["regions"]}
    assert rows["EL3"]["name"] == "Περιφέρεια Αττικής"
    assert rows["EL3"]["name_source"] == "el"
    assert rows["PT1A0"]["name"].startswith("Μητροπολιτική")
    # An unknown language is English, not a 422: this endpoint is public.
    assert client.get("/nuts/regions?codes=EL3&lang=zz").json()[
        "regions"][0]["name"] == "Attica Region"


def test_regions_can_drop_the_per_language_map(client):
    """It is most of the payload, and a caller showing one language pays for
    23 others otherwise."""
    full = client.get("/nuts/regions?limit=5000")
    lean = client.get("/nuts/regions?limit=5000&lang=en&names=none")
    lean_row = lean.json()["regions"][0]
    assert lean_row["names"] is None and lean_row["aliases"] is None
    assert lean_row["name"] and lean_row["name_latn"]      # still usable
    assert full.json()["regions"][0]["names"]
    assert len(lean.content) < len(full.content) / 3


def test_regions_narrow_to_the_codes_asked_for(client):
    """Labelling three regions on a feed card should not pull 1,798 rows."""
    body = client.get("/nuts/regions?codes=EL3,pt1a0, EL303 ").json()
    assert sorted(r["code"] for r in body["regions"]) == ["EL3", "EL303", "PT1A0"]
    assert body["total"] == 3
    # An explicit empty selection means none, not everything.
    assert client.get("/nuts/regions?codes=").json()["regions"] == []


def test_regions_cap_the_depth(client):
    """`max_level=0` is the country list a picker opens with."""
    countries = client.get("/nuts/regions?max_level=0&limit=5000").json()
    assert countries["total"] == 39
    assert {r["level"] for r in countries["regions"]} == {0}
    shallow = client.get("/nuts/regions?max_level=2&limit=5000").json()
    assert max(r["level"] for r in shallow["regions"]) == 2


def test_search_caps_the_depth_too(client):
    matches = client.get("/nuts/search?q=att&max_level=1").json()["matches"]
    assert matches and all(m["level"] <= 1 for m in matches)


def test_regions_reject_a_nonsense_filter(client):
    assert client.get("/nuts/regions?level=9").status_code == 422
    assert client.get("/nuts/regions?limit=99999").status_code == 422
    assert client.get("/nuts/regions?names=some").status_code == 422


# ── GET /nuts/regions/{code} ───────────────────────────────────


def test_one_region_derives_its_hierarchy(client):
    """NUTS codes nest by prefix, so ancestors and children need no lookup
    table — and a caller should not need a second request for them."""
    body = client.get("/nuts/regions/EL303").json()
    assert [a["code"] for a in body["ancestors"]] == ["EL30", "EL3", "EL"]
    assert body["children"] == []
    parent = client.get("/nuts/regions/EL30").json()
    assert "EL303" in [c["code"] for c in parent["children"]]


def test_one_region_is_case_insensitive_and_404s_honestly(client):
    assert client.get("/nuts/regions/el3").json()["code"] == "EL3"
    missing = client.get("/nuts/regions/ZZ9")
    assert missing.status_code == 404
    assert "ZZ9" in missing.json()["detail"]


# ── GET /nuts/search ───────────────────────────────────────────


def test_search_resolves_a_name_in_any_language(client):
    for term, code in [("Attica", "EL3"), ("Attiki", "EL3"), ("Αττική", "EL3"),
                       ("αττικη", "EL3"), ("Lisbonne", "PT1A0"),
                       ("Lissabon", "PT1A0"), ("Bergstrasse", "DE715")]:
        codes = [m["code"] for m in client.get(f"/nuts/search?q={term}").json()["matches"]]
        assert code in codes, f"{term} should find {code}, got {codes[:4]}"


def test_search_says_what_matched_and_in_which_language(client):
    """A hit a caller cannot explain is a hit they cannot trust: "Lisbonne"
    finding PT1A0 makes sense once you see it matched the French name."""
    match = next(m for m in client.get("/nuts/search?q=Lisbonne&lang=fr").json()["matches"]
                 if m["code"] == "PT1A0")
    assert match["matched"] == "Aire métropolitaine de Lisbonne"
    assert match["matched_language"] == "fr"
    assert match["matched_kind"] == "name"
    assert match["name_native"] == "Grande Lisboa"


def test_search_ranks_the_requested_language_first(client):
    """Asking in Greek should not put an English label above a Greek one."""
    matches = client.get("/nuts/search?q=attik&lang=el&limit=5").json()["matches"]
    top = matches[0]
    assert top["matched_language"] in (None, "el") or top["matched_kind"] != "name"
    assert top["rank"] <= matches[-1]["rank"]


def test_search_takes_a_code_and_ranks_the_exact_one_first(client):
    body = client.get("/nuts/search?q=EL3").json()
    assert body["matches"][0]["code"] == "EL3"
    assert body["matches"][0]["matched_kind"] == "code"
    assert body["total"] > 1      # EL30, EL301… also start with EL3


def test_search_reports_one_hit_per_region(client):
    """A region matching through five of its names is still one answer."""
    matches = client.get("/nuts/search?q=lisboa&limit=50").json()["matches"]
    codes = [m["code"] for m in matches]
    assert len(codes) == len(set(codes))


def test_search_filters_by_level_and_country(client):
    body = client.get("/nuts/search?q=lisboa&level=3&country=PT").json()
    assert body["matches"] and all(m["level"] == 3 and m["country"] == "PT"
                                  for m in body["matches"])


def test_search_is_honest_about_a_miss(client):
    body = client.get("/nuts/search?q=atlantis").json()
    assert body["total"] == 0 and body["matches"] == []


def test_search_resolves_the_display_language(client):
    assert client.get("/nuts/search?q=EL3&lang=pt-BR").json()["lang"] == "pt"
    assert client.get("/nuts/search?q=EL3&lang=klingon").json()["lang"] == "en"
    assert client.get("/nuts/search?q=").status_code == 422


# ── bulk ───────────────────────────────────────────────────────


def test_csv_is_long_format_and_loads(client):
    """One row per name, so it lands in a spreadsheet or a table without
    anyone writing JSON-flattening code first."""
    response = client.get("/nuts/regions.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) > 30_000
    el3 = [r for r in rows if r["code"] == "EL3"]
    assert {"native", "name"} <= {r["kind"] for r in el3}
    greek = next(r for r in el3 if r["language"] == "el")
    assert greek["name"] == "Περιφέρεια Αττικής"
    native = next(r for r in el3 if r["kind"] == "native")
    assert native["language"] == "" and native["name"] == "Αττική"


def test_bulk_json_is_the_artifact_itself(client):
    """Whoever vendors a copy should get what we serve, provenance included."""
    body = client.get("/nuts/gazetteer.json").json()
    assert body["nuts_version"] == "2024"
    assert set(body["sources"]) == {"eurostat", "euvoc", "wikidata"}
    assert len(body["regions"]) == 1798
    assert body["regions"]["EL3"]["labels"]["fr"]


# ── headers ────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/nuts", "/nuts/regions?limit=1",
                                  "/nuts/regions/EL3", "/nuts/search?q=EL3",
                                  "/nuts/regions.csv", "/nuts/gazetteer.json"])
def test_every_endpoint_is_cacheable_and_cross_origin(client, path):
    """A reference API that a browser cannot read, or that cannot be cached,
    is inconvenient in exactly the way this one exists not to be."""
    response = client.get(path)
    assert response.status_code == 200
    assert "public" in response.headers["cache-control"]
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["etag"].startswith('W/"nuts-2024-')
