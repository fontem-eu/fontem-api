"""Tests for the ECI petitions loader (petitions plan P0)."""
from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

from fontem_event_schemas import builders
from fontem_event_schemas.validate import validate

from src.etl import load_eu_petitions as mod
from src.etl.load_eu_petitions import (
    _iso,
    normalize_answer_ref,
    parse_initiative,
    plain_text,
    without_emails,
    write_artifact,
)

ENTRY = {
    "id": 8807, "pubRegNum": "ECI(2024)000007", "status": "ANSWERED",
    "title": "Stop Destroying Videogames", "totalSupporters": 1294188,
    "latestUpdateDate": "16/06/2026 10:00",
}

DETAIL = {
    "id": 8807, "comRegNum": "ECI(2024)000007", "status": "ANSWERED",
    "registrationDate": "19/06/2024", "deadline": "",
    "latestUpdateDate": "16/06/2026 10:00",
    "progress": [
        {"name": "REGISTERED", "date": "19/06/2024"},
        {"name": "COLLECTION_START_DATE", "date": "31/07/2024"},
        {"name": "CLOSED", "date": "31/07/2025"},
        {"name": "SUBMITTED", "date": "26/01/2026"},
        {"name": "ANSWERED", "date": "16/06/2026"},
    ],
    "members": [
        {"type": "REPRESENTATIVE", "fullName": "Daniel ONDRUSKA",
         "email": "daniel@example.org", "residenceCountry": "de",
         "privacyApplied": False},
        {"type": "SUBSTITUTE", "fullName": "Hidden Person",
         "email": "hidden@example.org", "privacyApplied": True},
    ],
    "funding": {"sponsors": [
        {"name": "Volunteers", "amount": 0},
        {"name": "Some Org", "amount": 1500.5},
    ]},
    "linguisticVersions": [
        {"languageCode": "EN", "title": "Stop Destroying Videogames",
         "objectives": "o" * 900,
         "supportLink": "https://eci.ec.europa.eu/045/public/?lg=en",
         "commissionDecision": {
             "celex": "32024D1824",
             "url": "http://eur-lex.europa.eu/...32024D1824",
         }},
    ],
    "answer": {"decisionDate": "16/06/2026", "links": [
        {"defaultName": "COMMUNICATION",
         "defaultLink": "https://citizens-initiative.europa.eu/document/"
                        "download/xyz_en?filename=C_2026_4110_EN.pdf"},
    ]},
}


def test_iso_dates():
    assert _iso("19/06/2024") == "2024-06-19"
    assert _iso("07/07/2026 16:01") == "2026-07-07"
    assert _iso("") is None
    assert _iso("garbage") is None


def test_normalize_answer_ref():
    assert normalize_answer_ref("C_2026_4110_EN.pdf") == "C(2026)4110"
    assert normalize_answer_ref("...filename=C_2026_0411_EN.pdf") == "C(2026)411"
    assert normalize_answer_ref("no doc here") is None


def test_parse_initiative_full():
    row = parse_initiative(ENTRY, DETAIL)
    assert row["petition_id"] == "ECI(2024)000007"
    assert row["status"] == "ANSWERED"
    assert row["registration_date"] == "2024-06-19"
    assert row["collection_start_date"] == "2024-07-31"
    assert row["answered_date"] == "2026-06-16"
    assert row["total_supporters"] == 1294188
    assert row["registration_decision_celex"] == "32024D1824"
    assert row["answer_refs"] == ["C(2026)4110"]
    assert row["funding_total_eur"] == 1500.5
    assert row["funding_sponsor_count"] == 2
    assert len(row["objectives"]) == 900          # in full: the 500 cut is gone


def test_privacy_applied_members_skipped_and_no_emails():
    row = parse_initiative(ENTRY, DETAIL)
    assert row["organizer_names"] == ["Daniel ONDRUSKA"]
    assert row["organizer_roles"] == ["REPRESENTATIVE"]
    assert "Hidden Person" not in str(row)
    assert "@" not in str(row.get("organizer_names"))
    assert "example.org" not in str(row)


def test_parsed_row_builds_valid_event():
    row = parse_initiative(ENTRY, DETAIL)
    payload = builders.upsert_petition(**row)
    validate("UpsertPetition", 1, payload)


def test_fetch_register_offset_pagination(monkeypatch):
    """The register's first path segment is an OFFSET — the fetcher must
    step by PAGE_SIZE and dedup, never re-fetch overlapping windows."""
    all_entries = [{"id": i, "pubRegNum": f"ECI(2026){i:06d}"}
                   for i in range(7)]
    calls = []

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    def fake_get(url, **_kwargs):
        if "details" in url:
            return _Resp({})
        offset = int(url.rstrip("/").split("/")[-2])
        calls.append(offset)
        page = all_entries[offset:offset + 3]
        return _Resp({"recordsFound": 7, "entries": page})

    monkeypatch.setattr(mod, "get_with_retry", fake_get)
    monkeypatch.setattr(mod, "PAGE_SIZE", 3)
    monkeypatch.setattr(mod, "DETAIL_PACE_S", 0)
    snap = mod.fetch_register()
    ids = [e["id"] for e in snap["entries"]]
    assert ids == [0, 1, 2, 3, 4, 5, 6]
    assert calls[:3] == [0, 3, 6]


# ── What a petition carries: the register's own initiative, three of its
# 24 language versions kept, organisers replaced by synthetic names.

ANSWERED = json.loads((Path(__file__).parent / "fixtures" / "eci"
                       / "answered_initiative.json").read_text(encoding="utf-8"))
ANSWERED_ENTRY = {"id": ANSWERED["id"], "pubRegNum": ANSWERED["comRegNum"],
                  "status": ANSWERED["status"], "title": "SAVE CRUELTY FREE COSMETICS",
                  "totalSupporters": 1217916}


def _petition(detail=None):
    row = parse_initiative(ANSWERED_ENTRY, detail or ANSWERED)
    validate("UpsertPetition", 1, builders.upsert_petition(**row))
    return row


def test_a_petition_reads_in_every_language_the_register_publishes():
    row = _petition()
    assert set(row["versions"]) == {"en", "de", "fr"}
    assert row["versions"]["fr"]["title"].startswith("POUR DES COSMÉTIQUES SANS CRUAUTÉ")
    assert row["versions"]["de"]["objectives"].startswith("Mit dem EU-Verbot")
    assert row["title_lang"] == "en"            # the version it was registered in


def test_the_original_language_is_the_one_the_register_marks():
    detail = copy.deepcopy(ANSWERED)
    for v in detail["linguisticVersions"]:
        v["original"] = v["languageCode"] == "FR"
    row = _petition(detail)
    assert row["title_lang"] == "fr"
    assert row["title"].startswith("SAVE CRUELTY FREE COSMETICS")   # top level stays English


def test_objectives_and_annex_arrive_in_full_as_plain_text():
    """They were cut at 500 raw characters, HTML and all: 129 of 136
    initiatives were truncated, 41 of them inside unbalanced markup."""
    row = _petition()
    en = next(v for v in ANSWERED["linguisticVersions"] if v["languageCode"] == "EN")
    assert len(row["objectives"]) > 500 and "<" not in row["objectives"]
    assert row["objectives"] == plain_text(en["objectives"])
    assert row["annex_text"].startswith("The World-Leading EU Cosmetics")


def test_html_becomes_lines_and_bullets():
    assert plain_text("<p>We ask the Commission to:</p><ul><li>ban &amp; phase out</li>"
                      "<li>protect</li></ul><p>Thanks<br/>all</p>") == (
        "We ask the Commission to:\n• ban & phase out\n• protect\nThanks\nall")
    assert plain_text("  ") is None and plain_text(None) is None


def test_signatures_per_country_milestones_and_answer_documents_are_kept():
    row = _petition()
    assert row["verified_supporters"] == 1217916
    entries = ANSWERED["submission"]["entry"]
    assert row["verified_countries"] == [e["countryCodeType"] for e in entries]
    assert row["verified_counts"] == [e["total"] for e in entries]
    assert row["submitted_date"] == "2023-01-25" and row["answered_date"] == "2023-07-25"
    assert row["categories"] == [c["categoryType"] for c in ANSWERED["categories"]]
    assert row["answer_communication_url"].startswith("https://ec.europa.eu/transparency/")
    assert row["answer_press_release_url"] and row["answer_follow_up_url"]
    assert row["register_id"] == 1295
    assert row["register_url"] == ("https://citizens-initiative.europa.eu/initiatives/details/"
                                   "2021/000006_en")


def test_answer_references_in_the_documents_register_are_recognised():
    """Only 4 of 14 answered initiatives had one: links of the form
    ...?ref=C(2023)5041 were not read."""
    assert normalize_answer_ref("https://ec.europa.eu/transparency/documents-register/"
                                "detail?ref=C(2021)4747&lang=en") == "C(2021)4747"
    assert _petition()["answer_refs"]


def test_each_sponsor_is_kept_with_its_amount():
    row = _petition()
    sponsors = ANSWERED["funding"]["sponsors"]
    assert row["sponsor_names"] == [sp["name"] for sp in sponsors]
    assert row["sponsor_amounts_eur"] == [float(sp["amount"]) for sp in sponsors]
    assert row["funding_total_eur"] == float(sum(sp["amount"] for sp in sponsors))


def test_organisers_without_contacts_and_without_their_data_protection_officer():
    row = _petition()
    assert row["organizer_names"] == ["Alex EXAMPLE", "Sam EXAMPLE", "Kim EXAMPLE"]
    assert row["representative_country"] == "de"
    assert "Data PROTECTION" not in str(row) and "Hidden PERSON" not in str(row)
    assert "@" not in json.dumps(row)


def test_the_snapshot_kept_on_the_share_has_no_email_addresses(tmp_path):
    """296 organiser e-mails were written to the NFS share every day."""
    snapshot = {"entries": [ANSWERED_ENTRY], "details": {str(ANSWERED["id"]): ANSWERED}}
    path = write_artifact(without_emails(snapshot), str(tmp_path))
    kept = gzip.decompress(Path(path).read_bytes()).decode("utf-8")
    assert "@example.org" not in kept and "Alex EXAMPLE" in kept
    assert "alex@example.org" in json.dumps(ANSWERED)            # the input is untouched
