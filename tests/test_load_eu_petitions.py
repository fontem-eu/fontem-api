"""Tests for the ECI petitions loader (petitions plan P0)."""
from __future__ import annotations

from fontem_event_schemas import builders
from fontem_event_schemas.validate import validate

from src.etl import load_eu_petitions as mod
from src.etl.load_eu_petitions import (
    _iso,
    normalize_answer_ref,
    parse_initiative,
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
    assert len(row["objectives"]) == 500


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
