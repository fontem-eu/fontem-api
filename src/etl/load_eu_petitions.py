"""Load the European Citizens' Initiative register into the event log.

P0 of the petitions plan (docs/roadmap/petitions-plan.md in gitops).
Artifact-first, house doctrine: the full register (search index + one
detail document per initiative) is snapshotted to a dated, checksummed
artifact on the NFS share FIRST; only then are ``UpsertPetition`` events
emitted. Runs daily — petitions freshness is an explicit requirement.

Supporter counts are also written as daily snapshot rows into the events
store (``events.petition_supporters``, same precedent as
``events.dq_result``) so momentum charts have a time series; the latest
count rides on the node.

GDPR note: organizer names, roles and residence countries are republished
from the EU's own public register (Art 6(1)(e)); e-mail addresses are
NEVER carried, and members flagged ``privacyApplied`` upstream are
skipped entirely. Data-subject requests reach Fontem at gdpr@fontem.eu.

Usage:
    python -m src.etl.load_eu_petitions            # live fetch + emit
    python -m src.etl.load_eu_petitions --file X   # replay an artifact
"""

from __future__ import annotations


import argparse
import datetime
import gzip
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid

import psycopg
from fontem_event_schemas import builders
from fontem_events import EventLog
from src.etl.data_description import DataDescription

from ._http_retry import get_with_retry

DESCRIPTION = DataDescription(
    producer="load_eu_petitions",
    label="European Citizens' Initiatives",
    theme="influence",
    summary="European Citizens' Initiatives, their signature counts and outcomes.",
    entities=(
        "Initiative",
    ),
    coverage="The official ECI register.",
    upstream="ECI register",
    update_freq="weekly",
    answers=(
        "Which citizens' initiatives reached the Commission, and how many signatures they gathered",
    ),
)


logger = logging.getLogger(__name__)

# NOTE: the first numeric path segment is an OFFSET (entry index), not a
# page number — /ALL/EN/1/50 returns entries 1..50, overlapping /0/50.
SEARCH_URL = (
    "https://register.eci.ec.europa.eu/core/api/register/search/ALL/EN/{offset}/{size}"
)
DETAIL_URL = "https://register.eci.ec.europa.eu/core/api/register/details/{id}"
PAGE_SIZE = 50
DETAIL_PACE_S = 0.4

SYSTEM = "eu-eci"

# Answer/decision document filenames look like C_2026_4110_EN.pdf —
# normalise to the citable form C(2026)4110.
_CDOC_RE = re.compile(r"C[_-](\d{4})[_-](\d{1,5})")

_MILESTONE_FIELDS = {
    "REGISTERED": "registration_date",
    "COLLECTION_START_DATE": "collection_start_date",
    "CLOSED": "closed_date",
    "SUBMITTED": "submitted_date",
    "ANSWERED": "answered_date",
}

SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS events.petition_supporters (
    system        text        NOT NULL,
    petition_id   text        NOT NULL,
    snapshot_date date        NOT NULL,
    supporters    bigint      NOT NULL,
    status        text,
    PRIMARY KEY (system, petition_id, snapshot_date)
)
"""


def _iso(d: str | None) -> str | None:
    """Register dates are DD/MM/YYYY (sometimes with a time) → ISO date."""
    if not d:
        return None
    head = d.strip().split(" ")[0]
    try:
        return datetime.datetime.strptime(head, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def normalize_answer_ref(name_or_link: str) -> str | None:
    """C_2026_4110_EN.pdf / …C-2026-4110… → ``C(2026)4110``."""
    m = _CDOC_RE.search(name_or_link or "")
    if not m:
        return None
    return f"C({m.group(1)}){int(m.group(2))}"


def parse_initiative(entry: dict, detail: dict) -> dict:  # pylint: disable=too-many-locals
    """One register entry + its detail document → UpsertPetition kwargs."""
    en = None
    for v in detail.get("linguisticVersions") or []:
        if v.get("languageCode") == "EN":
            en = v
            break
        if en is None:
            en = v
    en = en or {}

    milestones: dict[str, str] = {}
    for step in detail.get("progress") or []:
        field = _MILESTONE_FIELDS.get(step.get("name") or "")
        if field and (iso := _iso(step.get("date"))):
            milestones[field] = iso

    names: list[str] = []
    roles: list[str] = []
    countries: list[str] = []
    for m in detail.get("members") or []:
        if m.get("privacyApplied"):
            continue
        name = (m.get("fullName") or "").strip()
        if not name:
            continue
        names.append(name)
        roles.append((m.get("type") or "").strip())
        countries.append((m.get("residenceCountry") or "").strip())

    funding = detail.get("funding") or {}
    sponsors = funding.get("sponsors") or []
    funding_total = float(sum(s.get("amount") or 0 for s in sponsors))

    answer = detail.get("answer") or {}
    answer_refs: list[str] = []
    for link in answer.get("links") or []:
        ref = normalize_answer_ref(
            link.get("defaultLink") or link.get("defaultName") or ""
        )
        if ref and ref not in answer_refs:
            answer_refs.append(ref)

    decision = (en.get("commissionDecision") or {}) if isinstance(
        en.get("commissionDecision"), dict) else {}

    out = {
        "system": SYSTEM,
        "petition_id": detail.get("comRegNum") or entry.get("pubRegNum"),
        "title": (en.get("title") or entry.get("title") or "").strip() or None,
        "status": detail.get("status") or entry.get("status"),
        "objectives": ((en.get("objectives") or "")[:500]) or None,
        "collection_deadline": _iso(detail.get("deadline")),
        "total_supporters": int(entry.get("totalSupporters") or 0),
        "support_link": en.get("supportLink") or None,
        "organizer_names": names or None,
        "organizer_roles": roles or None,
        "organizer_countries": countries or None,
        "funding_total_eur": funding_total,
        "funding_sponsor_count": len(sponsors),
        "registration_decision_celex": decision.get("celex") or None,
        "answer_refs": answer_refs or None,
        "answered_date": _iso(answer.get("decisionDate")),
        "latest_update": _iso(detail.get("latestUpdateDate")
                              or entry.get("latestUpdateDate")),
    }
    out.update(milestones)
    # registration date from progress wins; fall back to the detail field
    out.setdefault("registration_date", _iso(detail.get("registrationDate")))
    return {k: v for k, v in out.items() if v is not None}


def fetch_register() -> dict:
    """Full register snapshot: search pages + one detail per initiative."""
    entries: list[dict] = []
    seen: set = set()
    offset = 0
    while True:
        resp = get_with_retry(
            SEARCH_URL.format(offset=offset, size=PAGE_SIZE), timeout=60,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("entries") or []
        fresh = [e for e in batch if e["id"] not in seen]
        seen.update(e["id"] for e in fresh)
        entries.extend(fresh)
        if len(entries) >= int(data.get("recordsFound") or 0) or not batch:
            break
        offset += PAGE_SIZE
    details = {}
    for e in entries:
        time.sleep(DETAIL_PACE_S)
        resp = get_with_retry(
            DETAIL_URL.format(id=e["id"]), timeout=60, follow_redirects=True,
        )
        resp.raise_for_status()
        details[str(e["id"])] = resp.json()
    return {"fetched_at": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(), "entries": entries, "details": details}


def write_artifact(snapshot: dict, data_dir: str) -> str:
    """Gzip the snapshot with a sha256 manifest next to it."""
    os.makedirs(data_dir, exist_ok=True)
    day = datetime.date.today().isoformat()
    path = os.path.join(data_dir, f"eci-{day}.json.gz")
    raw = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    with gzip.open(path, "wb") as fh:
        fh.write(raw)
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    with open(path.replace(".json.gz", ".manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"file": os.path.basename(path), "sha256": digest,
                   "initiatives": len(snapshot.get("entries") or [])}, fh)
    return path


def write_snapshots(rows: list[dict]) -> int:
    """Daily supporter-count rows into events.petition_supporters."""
    dsn = os.environ.get("EVENTS_DATABASE_URL", "")
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    if not dsn:
        logger.warning("EVENTS_DATABASE_URL unset — skipping snapshots")
        return 0
    today = datetime.date.today()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(SNAPSHOT_DDL)
            cur.executemany(
                """INSERT INTO events.petition_supporters
                   (system, petition_id, snapshot_date, supporters, status)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (system, petition_id, snapshot_date)
                   DO UPDATE SET supporters = EXCLUDED.supporters,
                                 status = EXCLUDED.status""",
                [(SYSTEM, r["petition_id"], today,
                  r.get("total_supporters") or 0, r.get("status"))
                 for r in rows],
            )
        conn.commit()
    return len(rows)


def main(argv=None):  # pylint: disable=too-many-locals
    """CLI entry point — events into events.entity_events, sinks project."""
    parser = argparse.ArgumentParser(description="Load the ECI register")
    parser.add_argument("--file", help="Replay a snapshot artifact (json.gz)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.file:
        with gzip.open(args.file, "rb") as fh:
            snapshot = json.load(fh)
        logger.info("Replaying artifact %s", args.file)
    else:
        snapshot = fetch_register()
        data_dir = os.path.join(
            os.environ.get("PETITIONS_DATA_DIR", "/edgar-data/petitions"),
            "eci",
        )
        path = write_artifact(snapshot, data_dir)
        logger.info("Artifact written: %s", path)

    by_id = snapshot["details"]
    rows = []
    for entry in snapshot["entries"]:
        detail = by_id.get(str(entry["id"]))
        if not detail:
            logger.warning("No detail for %s — skipped", entry.get("pubRegNum"))
            continue
        rows.append(parse_initiative(entry, detail))
    logger.info("Parsed %d initiatives", len(rows))

    log = EventLog.from_env()
    with log.batch(uuid.uuid4(), producer="load_eu_petitions") as emit:
        for row in rows:
            sys_camel = SYSTEM.replace("-", "_").title().replace("_", "")
            iri = (f"http://data.fontem.eu/id/{sys_camel}Petition/"
                   f"{row['petition_id']}")
            emit.upsert("UpsertPetition", iri=iri, domain="petitions",
                        payload=builders.upsert_petition(**row))
    logger.info("Emitted %d UpsertPetition events", len(rows))

    n = write_snapshots(rows)
    logger.info("Wrote %d supporter snapshots", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
