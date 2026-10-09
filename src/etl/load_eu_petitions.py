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

Every official language version travels (2026-10-09 source review): the
register publishes each initiative in up to 24 languages, one of them the
original, with the objectives and annex in full. The texts are converted
from the register's HTML to plain text, paragraphs and list items on lines
of their own.

GDPR note: organizer names, roles and residence countries are republished
from the EU's own public register (Art 6(1)(e)); e-mail addresses are
NEVER carried — not in events, and not in the snapshot either — members
flagged ``privacyApplied`` upstream are skipped entirely, and so are
data-protection officers, who are contacts, not organisers. Data-subject
requests reach Fontem at gdpr@fontem.eu.

Usage:
    python -m src.etl.load_eu_petitions            # live fetch + emit
    python -m src.etl.load_eu_petitions --file X   # replay an artifact
"""

from __future__ import annotations


import argparse
import copy
import datetime
import gzip
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import uuid
from html.parser import HTMLParser

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

# Answer/decision documents are referenced as C_2026_4110_EN.pdf in file
# names and as ref=C(2021)4747 in the Commission's document register;
# both normalise to the citable form C(2026)4110.
_CDOC_RE = re.compile(r"C(?:[_-](\d{4})[_-]|\((\d{4})\)\s*)(\d{1,5})")

#: Every step of the register's procedure, as the date it was reached.
_MILESTONE_FIELDS = {
    "REGISTERED": "registration_date",
    "COLLECTION_START_DATE": "collection_start_date",
    "ONGOING": "ongoing_date",
    "CLOSED": "closed_date",
    "VERIFICATION": "verification_date",
    "SUBMITTED": "submitted_date",
    "ANSWERED": "answered_date",
    "WITHDRAWN": "withdrawn_date",
    "REJECTED": "rejected_date",
    "INSUFFICIENT_SUPPORT": "insufficient_support_date",
    "INSUFFICIENT_SUPPORT_AFTER_VERIFICATION": "insufficient_support_after_verification_date",
}

#: The Commission's answer documents, by the register's name for them.
_ANSWER_LINKS = {
    "COMMUNICATION": "answer_communication_url",
    "ANNEX": "answer_annex_url",
    "PRESS_RELEASE": "answer_press_release_url",
    "FOLLOW_UP": "answer_follow_up_url",
}

#: Members listed for contact, not as organisers.
_NOT_ORGANISERS = frozenset({"DPO"})

REGISTER_PAGE = "https://citizens-initiative.europa.eu/initiatives/details/{year}/{number}_en"

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
    """C_2026_4110_EN.pdf / …C-2026-4110… / …ref=C(2021)4747… → ``C(2026)4110``."""
    m = _CDOC_RE.search(name_or_link or "")
    if not m:
        return None
    return f"C({m.group(1) or m.group(2)}){int(m.group(3))}"


class _PlainText(HTMLParser):
    """Text of the register's HTML: block elements and line breaks end a
    line, list items start with a bullet."""

    _BLOCKS = frozenset({"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4",
                         "h5", "h6", "tr", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag):
        if tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(markup: str | None) -> str | None:
    """The register's HTML as plain text, in full; None when empty."""
    if not markup or not markup.strip():
        return None
    parser = _PlainText()
    parser.feed(markup)
    parser.close()
    lines = (" ".join(html.unescape(line).split()) for line in "".join(parser.parts).split("\n"))
    text = "\n".join(line for line in lines if line and line != "•")
    return text or None


def _version(v: dict) -> dict:
    """One language version's texts and links, plain text in full."""
    decision = v.get("commissionDecision") if isinstance(v.get("commissionDecision"), dict) else {}
    out = {
        "title": (v.get("title") or "").strip() or None,
        "objectives": plain_text(v.get("objectives")),
        "annex_text": plain_text(v.get("annexText")),
        "treaties": plain_text(v.get("treaties")),
        "website": (v.get("website") or "").strip() or None,
        "support_link": (v.get("supportLink") or "").strip() or None,
        "decision_url": (decision.get("url") or "").strip() or None,
    }
    return {k: val for k, val in out.items() if val}


def _counts(block: dict | None, with_after: bool = False) -> dict:
    entries = (block or {}).get("entry") or []
    out = {"countries": [str(e.get("countryCodeType") or "") for e in entries],
           "counts": [int(e.get("total") or 0) for e in entries]}
    if with_after:
        out["after"] = [bool(e.get("afterSubmission")) for e in entries]
    return out


def _register_page(petition_id: str | None) -> str | None:
    m = re.match(r"ECI\((\d{4})\)(\d+)$", petition_id or "")
    return REGISTER_PAGE.format(year=m.group(1), number=m.group(2)) if m else None


def parse_initiative(entry: dict, detail: dict) -> dict:  # pylint: disable=too-many-locals
    """One register entry + its detail document → UpsertPetition kwargs.

    The top-level texts are the English version, which the register
    publishes for every initiative; ``versions`` carries every language,
    and ``title_lang`` names the one it was registered in."""
    by_lang = {(v.get("languageCode") or "").lower(): v
               for v in detail.get("linguisticVersions") or [] if v.get("languageCode")}
    original = next((v for v in detail.get("linguisticVersions") or [] if v.get("original")), None)
    en = by_lang.get("en") or original or next(iter(by_lang.values()), {})
    versions = {code: _version(v) for code, v in by_lang.items() if re.match(r"^[a-z]{2}$", code)}
    english = _version(en)

    milestones: dict[str, str] = {}
    for step in detail.get("progress") or []:
        field = _MILESTONE_FIELDS.get(step.get("name") or "")
        if field and (iso := _iso(step.get("date"))):
            milestones[field] = iso

    organisers = [m for m in detail.get("members") or []
                  if not m.get("privacyApplied") and (m.get("fullName") or "").strip()
                  and (m.get("type") or "") not in _NOT_ORGANISERS]
    representative = next((m for m in organisers if m.get("type") == "REPRESENTATIVE"), {})

    funding = detail.get("funding") or {}
    sponsors = funding.get("sponsors") or []
    answer = detail.get("answer") or {}
    answer_refs: list[str] = []
    answer_links: dict[str, str] = {}
    for link in answer.get("links") or []:
        url = (link.get("defaultLink") or "").strip()
        if (field := _ANSWER_LINKS.get(link.get("defaultName") or "")) and url:
            answer_links.setdefault(field, url)
        ref = normalize_answer_ref(url or link.get("defaultName") or "")
        if ref and ref not in answer_refs:
            answer_refs.append(ref)
    decision = en.get("commissionDecision")
    decision = decision if isinstance(decision, dict) else {}
    annex_doc = en.get("additionalDocument") or {}
    draft = en.get("draftLegal") or {}
    online, verified = _counts(detail.get("sosReport")), _counts(detail.get("submission"), True)
    petition_id = detail.get("comRegNum") or entry.get("pubRegNum")

    out = {
        "system": SYSTEM,
        "petition_id": petition_id,
        "title": english.get("title") or (entry.get("title") or "").strip() or None,
        "title_lang": ((original or {}).get("languageCode") or "").lower() or None,
        "status": detail.get("status") or entry.get("status"),
        "objectives": english.get("objectives"),
        "annex_text": english.get("annex_text"),
        "treaties": english.get("treaties"),
        "website": english.get("website"),
        "versions": versions or None,
        "categories": [c["categoryType"] for c in detail.get("categories") or []
                       if c.get("categoryType")] or None,
        "register_id": detail.get("id") or entry.get("id"),
        "register_url": _register_page(petition_id),
        "collection_deadline": _iso(detail.get("deadline")),
        "early_closure_date": _iso(detail.get("earlyClosureDate")),
        "partially_registered": detail.get("partiallyRegistered"),
        "total_supporters": int(entry.get("totalSupporters") or 0),
        "online_supporters": (detail.get("sosReport") or {}).get("totalSignatures"),
        "supporters_updated_at": _iso((detail.get("sosReport") or {}).get("updateDate")),
        "supporter_countries": online["countries"] or None,
        "supporter_counts": online["counts"] or None,
        "verified_supporters": (detail.get("submission") or {}).get("totalSignatures"),
        "verified_countries": verified["countries"] or None,
        "verified_counts": verified["counts"] or None,
        "verified_after_submission": verified["after"] or None,
        "support_link": english.get("support_link"),
        "organizer_names": [m["fullName"].strip() for m in organisers] or None,
        "organizer_roles": [(m.get("type") or "").strip() for m in organisers] or None,
        "organizer_countries": [(m.get("residenceCountry") or "").strip()
                                for m in organisers] or None,
        "representative_country": (representative.get("residenceCountry") or "").strip() or None,
        "funding_total_eur": float(sum(sp.get("amount") or 0 for sp in sponsors)),
        "funding_sponsor_count": len(sponsors),
        "funding_updated_at": _iso(funding.get("lastUpdate")),
        "funding_document_name": (funding.get("document") or {}).get("name"),
        "sponsor_names": [(sp.get("name") or "").strip() for sp in sponsors] or None,
        "sponsor_amounts_eur": [float(sp.get("amount") or 0) for sp in sponsors] or None,
        "sponsor_dates": [_iso(sp.get("date")) or "" for sp in sponsors] or None,
        "sponsor_private": [bool(sp.get("privateSponsor")) for sp in sponsors] or None,
        "sponsor_anonymized": [bool(sp.get("anonymized")) for sp in sponsors] or None,
        "sponsor_other_support": [(sp.get("otherSupport") or "").strip()
                                  for sp in sponsors] or None,
        "registration_decision_celex": decision.get("celex") or None,
        "registration_decision_url": decision.get("url") or None,
        "registration_decision_corrigendum": decision.get("corrigendum") or None,
        "annex_document_name": annex_doc.get("name"),
        "annex_document_id": annex_doc.get("id"),
        "draft_legal_act_name": draft.get("name"),
        "draft_legal_act_id": draft.get("id"),
        "answer_refs": answer_refs or None,
        "answered_date": _iso(answer.get("decisionDate")),
        "latest_update": _iso(detail.get("latestUpdateDate")
                              or entry.get("latestUpdateDate")),
        **answer_links,
    }
    out.update(milestones)
    # registration date from progress wins; fall back to the detail field
    out.setdefault("registration_date", _iso(detail.get("registrationDate")))
    return {k: v for k, v in out.items() if v is not None}


def without_emails(snapshot: dict) -> dict:
    """The snapshot as it is kept on the share: organisers' e-mail addresses
    removed, everything else as the register sent it."""
    kept = copy.deepcopy(snapshot)
    for detail in (kept.get("details") or {}).values():
        for member in detail.get("members") or []:
            member.pop("email", None)
    return kept


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
        snapshot = without_emails(fetch_register())
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
