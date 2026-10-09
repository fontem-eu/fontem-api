"""
EU Transparency Register → event log
======================================
Downloads the daily XML dump from the EU Transparency Register
and emits two kinds of events per registered lobbyist:

  1. ``UpsertDisclosure`` — system='eu-lobbying', disclosure_id=tr_id.
     The Lobbyist itself is the registrant (no parent Company), so
     ``company_gmr_id`` is omitted (relaxed schema). All Lobbyist
     fields ride in ``details``.
  2. ``UpsertRelationship`` — for each confident Lobbyist→Company
     match returned by the resolver, a 'represents' edge from the
     Disclosure IRI to the Company IRI.

Lobbyist→Company resolution stays the way it was: one POST to the
gmr-consolidator ``/resolve`` endpoint per lobbyist (name +
ISO-3 country). Tier-1/2/3 confident matches emit a relationship
event; ambiguous and Tier-4 fuzzy results are skipped — there's
no value in flooding the graph with 50-70%-false-positive edges
the way the previously-deleted in-cypher matcher did.

GDPR note: the Transparency Register includes named individual
representatives. Their data is republished here on the same lawful
basis as the upstream (Art 6(1)(e), public-interest task). Data-subject
requests reach Fontem at **gdpr@fontem.eu**. When a registrant drops
off the upstream daily dump the disclosure IRI is tombstoned; the
sink does not retain "last seen" entries.

What a registrant declares travels in full (2026-10-09 source review):
every free-text field without a cut, the financial block of each kind of
registrant (own interests: spend band and intermediaries; NGOs: budget,
funding sources, contributors; consultancies: revenue band and clients),
grants, staff, offices. Artifact-first, like the ECI loader: the day's XML
is snapshotted to the NFS share before any event is emitted, and a snapshot
can be replayed with --file. E-mail addresses inside free text are removed.

Usage:
    python -m src.etl.load_eu_lobbying              # download, snapshot, emit
    python -m src.etl.load_eu_lobbying --file X     # replay a snapshot
"""

from __future__ import annotations


import argparse
import datetime
import functools
import gzip
import hashlib
import json
import logging
import os
import re
import uuid
import xml.etree.ElementTree as ET
from typing import Any

import httpx
import psycopg
import pycountry
from fontem_event_schemas import builders
from fontem_events import EventLog

from src.etl._hooks import resolve_entity
from src.etl._http import HTTP_HEADERS
from src.etl.data_description import DataDescription

DESCRIPTION = DataDescription(
    producer="load_eu_lobbying",
    label="EU Lobbying",
    theme="influence",
    summary="Organisations registered to lobby the EU institutions, with declared spend.",
    entities=(
        "Lobbyist",
    ),
    coverage=(
        "Self-declared entries in the EU Transparency Register. Registration is not fully "
        "mandatory, and figures are as declared, not audited."
    ),
    upstream="EU Transparency Register",
    update_freq="weekly",
    answers=(
        "Who lobbies Brussels on a given interest, and what they declare spending",
        "Whether a company that wins public contracts also lobbies",
    ),
)


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TR_XML_URL = "https://transparency-register.europa.eu/odplastorganisationxml_en"
EMIT_CHUNK = 500

# Placeholder written into name fields when a lobbyist is tombstoned, so
# the kept history carries no personal data (GDPR).
_REDACTED = "[deregistered]"

#: The register's page for one registrant. The XML carries no link of its
#: own; `webSiteURL` is the organisation's site.
REGISTER_PAGE = ("https://transparency-register.europa.eu/search-register-or-update/"
                 "organisation-detail_en?id={tr_id}")

#: Register spellings pycountry does not resolve (2026-10-09: 7 of the 139
#: head-office countries). Kosovo takes the code EU bodies use.
_COUNTRY_OVERRIDES = {
    "TURKEY": "TUR", "PALESTINE (*)": "PSE", "BOSNIA-HERZEGOVINA": "BIH",
    "CONGO, DEMOCRATIC REPUBLIC OF": "COD", "LAOS, PEOPLE'S DEMOCRATIC REPUBLIC": "LAO",
    "KOSOVO (*)": "XKX", "RUSSIA, FEDERATION OF": "RUS",
}


@functools.lru_cache(maxsize=None)
def country_alpha3(name: str) -> str | None:
    """ISO 3166-1 alpha-3 for a register country name, as Company nodes
    carry it; None for a name nothing resolves. One table for every
    registrant: it used to be ISO-3 for most, ISO-2 for the US and the UK,
    and the raw name for 818."""
    key = (name or "").strip().upper()
    if not key:
        return None
    if key in _COUNTRY_OVERRIDES:
        return _COUNTRY_OVERRIDES[key]
    try:
        return pycountry.countries.lookup(key.title()).alpha_3
    except LookupError:
        try:
            return pycountry.countries.search_fuzzy(key.title())[0].alpha_3
        except LookupError:
            return None


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _free_text(text: str) -> str | None:
    """A registrant's free text as it reads, without e-mail addresses (a
    person's contact details have no business in a public profile)."""
    text = (text or "").strip()
    if not text or text.upper() in ("N/A", "NA", "-", "NOT APPLICABLE"):
        return None
    return _EMAIL.sub("[e-mail removed]", text)


def _website(url: str) -> str | None:
    """The organisation's site as a link: 'www.example.org' resolved against
    fontem.eu as a relative path."""
    url = (url or "").strip()
    if not url:
        return None
    return url if re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I) else f"https://{url}"


def _text(elem: ET.Element | None, path: str) -> str:
    """Extract text from an XML element at the given path."""
    if elem is None:
        return ""
    child = elem.find(path)
    return (child.text or "").strip() if child is not None else ""


def _num(elem: ET.Element | None, path: str, kind=float):
    """A number at ``path``, or None where the register states none."""
    raw = _text(elem, path)
    if not raw:
        return None
    try:
        return kind(float(raw))
    except ValueError:
        return None


def _band(elem: ET.Element | None) -> tuple[int | None, int | None]:
    """A CostRange's (min, max), well-ordered when both are present.

    The register doesn't validate self-reported ranges: registrants
    transpose the bounds now and then, so a transposed pair is put back in
    order. An open-top bracket (">= 10,000,000") has a min and no max:
    it is mirrored as [min, min], never read as max < min.
    """
    range_el = elem.find("range") if elem is not None else None
    low, high = _num(range_el, "min", int), _num(range_el, "max", int)
    if low and high:
        return min(low, high), max(low, high)
    if low:
        return low, low
    return low, high


def _absolute(elem: ET.Element | None) -> float | None:
    return _num(elem, "absoluteCost") if elem is not None else None


#: closedYear/@type -> what kind of registrant files it.
_FINANCIAL_TYPES = {
    "ClosedYearIntermediaryFinancialInformation": "own_interests",
    "ClosedYearNGOFinancialInformation": "ngo",
    "ClosedYearClientFinancialInformation": "consultancy",
}


def _financial(fin: ET.Element | None) -> dict[str, Any]:  # pylint: disable=too-many-locals
    """The financial block, whichever kind of registrant filed it, as
    scalars and parallel lists. Amounts are as declared: some are
    implausible (a €736bn budget) and are kept, not corrected."""
    out: dict[str, Any] = {}
    if fin is None:
        return out
    if (new := _text(fin, "newOrganisation")):
        out["new_organisation"] = new == "true"
    out["financial_complementary_info"] = _free_text(_text(fin, "complementaryInformation"))
    closed, current = fin.find("closedYear"), fin.find("currentYear")
    if closed is not None:
        out["financial_type"] = _FINANCIAL_TYPES.get(closed.get("type") or "")
        out["financial_year_start"] = _text(closed, "startDate")[:10] or None
        out["financial_year_end"] = _text(closed, "endDate")[:10] or None
        # Both ends of a band travel whenever either is stated, so a band
        # that loses its lower end overwrites the stale one in the sink.
        for field, tag in (("cost", "costs"), ("revenue", "totalAnnualRevenue")):
            low, high = _band(closed.find(tag))
            if low is not None or high is not None:
                out[f"{field}_min"], out[f"{field}_max"] = low or 0, high or 0
        out["total_budget_eur"] = _absolute(closed.find("totalBudget"))
        out["other_source_info"] = _free_text(_text(closed, "otherSourceInfo"))
        out["funding_sources"] = [_text(f, "source")
                                  for f in closed.findall("fundingSources/fundingSource")
                                  if _text(f, "source")]
        contributors = closed.findall("contributions/contributor")
        out["contributor_names"] = [_text(c, "name") for c in contributors]
        out["contributor_amounts_eur"] = [_absolute(c.find("amount")) or 0.0 for c in contributors]
        clients = closed.findall("clients/client")
        out["client_names"] = [_text(c, "name") for c in clients]
        out["client_proposals"] = [_free_text(_text(c, "proposal")) or "" for c in clients]
        bands = [_band(c.find("revenue")) for c in clients]
        out["client_revenue_min"] = [b[0] or 0 for b in bands]
        out["client_revenue_max"] = [b[1] or 0 for b in bands]
        intermediaries = closed.findall("intermediaries/intermediary")
        out["intermediary_names"] = [_text(i, "name") for i in intermediaries]
        costs = [_band(i.find("representationCosts")) for i in intermediaries]
        out["intermediary_cost_min"] = [c[0] or 0 for c in costs]
        out["intermediary_cost_max"] = [c[1] or 0 for c in costs]
        grants = closed.findall("grants/grant")
        out["grant_sources"] = [_text(g, "source") for g in grants]
        out["grant_amounts_eur"] = [_absolute(g.find("amount")) or 0.0 for g in grants]
    if current is not None:
        out["client_names_current"] = [_text(c, "name") for c in current.findall("clients/client")]
        out["intermediary_names_current"] = [
            _text(i, "name") for i in current.findall("intermediaries/intermediary")]
        grants = current.findall("grants/grant")
        out["grant_sources_current"] = [_text(g, "source") for g in grants]
        out["grant_amounts_eur_current"] = [_absolute(g.find("amount")) or 0.0 for g in grants]
    return out


#: A registrant who is a person, not an organisation: no postal details kept.
_INDIVIDUAL = "self-employed"


def _parse_entity(elem: ET.Element) -> dict[str, Any]:
    """One interestRepresentative as a flat dict: scalars and lists, None
    where the register states nothing, 0 where it states zero."""
    tr_id = _text(elem, "identificationCode")
    name_el, head, eu_office = elem.find("name"), elem.find("headOffice"), elem.find("EUOffice")
    members, structure = elem.find("members"), elem.find("structure")
    category = _text(elem, "registrationCategory")
    person = _INDIVIDUAL in category.lower()
    country = _text(head, "country")
    groupings = _free_text(_text(elem, "interOrUnofficalGroupings"))
    ent: dict[str, Any] = {
        "tr_id": tr_id,
        "name": _text(name_el, "originalName"),
        "name_latin": _text(name_el, "nameInLatinAlphabet") or None,
        "acronym": _text(elem, "acronym") or None,
        "category": category or None,
        "entity_form": _text(elem, "entityForm") or None,
        "interest_represented": _text(elem, "interestRepresented") or None,
        "website": _website(_text(elem, "webSiteURL")),
        "country": country or None,
        "country_iso": country_alpha3(country),
        "city": _text(head, "city") or None,
        "postcode": None if person else (_text(head, "postCode") or None),
        "eu_office_city": _text(eu_office, "city") or None,
        "eu_office_country": _text(eu_office, "country") or None,
        "eu_office_postcode": None if person else (_text(eu_office, "postCode") or None),
        "registration_date": _text(elem, "registrationDate")[:10] or None,
        "last_updated": _text(elem, "lastUpdateDate")[:10] or None,
        "goals": _free_text(_text(elem, "goals")),
        "eu_legislative_proposals": _free_text(_text(elem, "EULegislativeProposals")),
        "communication_activities": _free_text(_text(elem, "communicationActivities")),
        "eu_forums_platforms": _free_text(_text(elem, "EUSupportedForumsAndPlatforms")),
        "ep_intergroups": [g.strip() for g in (groupings or "").split(",") if g.strip()],
        "member_of": _free_text(_text(structure, "isMemberOf")),
        "organisation_members": _free_text(_text(structure, "organisationMembers")),
        "members_info": _free_text(_text(members, "infoMembers")),
        "levels_of_interest": [_text(level, "levelOfInterest") for level in
                               elem.findall("levelsOfInterest/levelOfInterest")
                               if _text(level, "levelOfInterest")],
        "interests": [_text(i, "name") for i in elem.findall("interests/interest")
                      if _text(i, "name")],
        "ep_passes": _num(elem, "EPAccreditedNumber", int),
        "persons_involved": _num(members, "members", int),
        "members_fte": (round(fte, 2) if (fte := _num(members, "membersFTE")) is not None
                        else None),
    }
    for share in (10, 25, 50, 75, 100):
        ent[f"persons_{share}pct"] = _num(members, f"members{share}Percent", int)
    ent.update(_financial(elem.find("financialData")))
    return ent


def parse_register(xml_bytes: bytes) -> tuple[dict, list[dict]]:
    """(export metadata, one dict per registrant) from the register's XML.
    It is XML 1.1 with control-character references the parser refuses."""
    xml_text = xml_bytes.decode("utf-8", errors="replace")
    xml_text = re.sub(r"&#x[0-9a-fA-F]{1,2};", " ", xml_text)
    xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)
    xml_text = re.sub(r"^<\?xml version=['\"]1\.1['\"]", "<?xml version='1.0'", xml_text)
    root = ET.fromstring(xml_text.encode("utf-8"))
    for elem in root.iter():
        elem.tag = elem.tag.split("}")[-1]
    meta = root.find("metaData")
    info = {"export_date": _text(meta, "exportDate"), "registrants": _text(meta, "numberOfIR")}
    result_list = root.find("resultList")
    entities = [] if result_list is None else [
        e for e in (_parse_entity(x) for x in result_list if x.tag == "interestRepresentative")
        if e["tr_id"]]
    return info, entities


def _disclosure_iri(tr_id: str) -> str:
    return f"http://data.fontem.eu/id/EuLobbyingDisclosure/{tr_id}"


#: Lists that name people or organisations by name: blanked when a
#: registrant leaves the register, like its own name.
_NAMED_LISTS = ("contributor_names", "client_names", "client_names_current",
                "intermediary_names", "intermediary_names_current")


def _details(ent: dict, match: tuple[str, str, float] | None) -> dict[str, object]:
    """What the disclosure carries: every stated value (0 and False are
    stated values), every non-empty list."""
    details: dict[str, object] = {
        k: v for k, v in ent.items()
        if k != "tr_id" and v is not None and v != "" and v != []
    }
    details["active"] = True
    if match is not None:
        details["registrant_match_tier"] = match[1]
        details["registrant_match_confidence"] = float(match[2])
    return details


def emit_lobbyist_disclosures(
    log: EventLog, entities: list[dict],
    matches: dict[str, tuple[str, str, float]] | None = None,
) -> int:
    """Emit one UpsertDisclosure per lobbyist.

    When the registrant resolves to a known Company (``matches[tr_id]``),
    set the disclosure's ``company_gmr_id`` so the sink materialises a
    ``FILED_BY`` edge from the disclosure's own upsert. That edge is built
    with the disclosure's full composite key, so unlike a typed REPRESENTS
    relationship (which can't address a composite-keyed :Disclosure and so
    100%-dropped at the sink) it actually attaches.

    The disclosure's ``url`` is its page on the register; the
    organisation's own site is ``details.website``.
    """
    matches = matches or {}
    emitted = 0
    todo = [e for e in entities if e.get("tr_id")]
    for start in range(0, len(todo), EMIT_CHUNK):
        with log.batch(uuid.uuid4(), producer="load_eu_lobbying") as emit:
            for ent in todo[start:start + EMIT_CHUNK]:
                match = matches.get(ent["tr_id"])
                emit.upsert(
                    "UpsertDisclosure",
                    iri=_disclosure_iri(ent["tr_id"]),
                    domain="eu_lobbying",
                    payload=builders.upsert_disclosure(
                        system="eu-lobbying",
                        disclosure_id=ent["tr_id"],
                        company_gmr_id=match[0] if match is not None else None,
                        disclosure_type="lobbyist-registration",
                        title=ent["name"][:200] or None,
                        url=REGISTER_PAGE.format(tr_id=ent["tr_id"]),
                        details=_details(ent, match),
                    ),
                )
                emitted += 1
    return emitted


def resolve_lobbyist_companies(
    entities: list[dict],
) -> tuple[dict[str, tuple[str, str, float]], dict]:
    """Resolve each lobbyist's registrant identity via the consolidator
    /resolve endpoint (name + ISO-3 country).

    Returns ``(matches, summary)`` where ``matches`` maps ``tr_id`` to
    ``(gmr_id, tier, confidence)`` for confident matches only. The caller
    sets that gmr_id as the disclosure's ``company_gmr_id`` to get a
    working FILED_BY edge. Ambiguous / no_match registrants are left as
    the standalone :Disclosure (which already *is* the lobbyist) — minting
    a duplicate Company for them would just clone that identity with no
    other source to cross-link to.
    """
    matches: dict[str, tuple[str, str, float]] = {}
    confident = 0
    ambiguous = 0
    no_match = 0
    for ent in entities:
        if not ent.get("tr_id") or not ent.get("name"):
            continue
        res = resolve_entity(
            entity_type="Company",
            name=ent["name"],
            country=ent.get("country_iso") or ent.get("country") or "",
        )
        if res is None:
            continue
        if res.hint == "matched" and res.match is not None:
            matches[ent["tr_id"]] = (
                res.match.gmr_id, res.match.tier, res.match.confidence,
            )
            confident += 1
        elif res.hint == "ambiguous":
            ambiguous += 1
        else:
            no_match += 1

    return matches, {
        "confident": confident, "ambiguous": ambiguous, "no_match": no_match,
    }


def _prior_disclosure_ids(dsn: str | None) -> set[str]:
    """tr_ids ever emitted for eu-lobbying, read from the loader's own
    event log. Loaders stay emit-only w.r.t. the graph, but the event
    store is our own output — reading it to diff the register snapshot
    against what we've seen before is fair game.
    """
    if not dsn:
        return set()
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    if "$(" in dsn:
        return set()
    prefix = _disclosure_iri("")
    ids: set[str] = set()
    # `domain` is indexed (entity_events_domain_seq); `producer` is not.
    # Filtering on producer alone was a parallel sequential scan of the
    # whole 61 GB / 72 M-row log — more than 33 minutes on 2026-09-21,
    # which is why every weekly run since 2026-09-07 died at its one-hour
    # deadline right after emitting. With the domain in the WHERE the
    # planner seeks the index; the same result takes about two minutes.
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT iri FROM events.entity_events "
            "WHERE domain = %s AND producer = %s",
            ("eu_lobbying", "load_eu_lobbying"),
        )
        for (iri,) in cur:
            if iri and iri.startswith(prefix):
                ids.add(iri[len(prefix):])
    return ids


def emit_deregistrations(
    log: EventLog, dropped_ids: set[str], deregistered_at: str,
) -> int:
    """Tombstone lobbyists that fell off the register. Keep the record
    and its interests (the political signal that matters), flip
    ``active`` false, and redact the name fields — the Transparency
    Register names natural persons, and once a registrant drops off the
    upstream lawful basis we retain trends, not identities (GDPR).
    The eu-lobbying upsert still carries the :Lobbyist label via the
    sink, and the partial SET leaves detail_interests/category/etc.
    untouched. Lists that name people or organisations are blanked too.
    """
    if not dropped_ids:
        return 0
    n = 0
    with log.batch(uuid.uuid4(), producer="load_eu_lobbying") as emit:
        for tr_id in sorted(dropped_ids):
            emit.upsert(
                "UpsertDisclosure",
                iri=_disclosure_iri(tr_id),
                domain="eu_lobbying",
                payload=builders.upsert_disclosure(
                    system="eu-lobbying",
                    disclosure_id=tr_id,
                    disclosure_type="lobbyist-registration",
                    title=_REDACTED,
                    details={
                        "name": _REDACTED,
                        "acronym": _REDACTED,
                        "name_latin": _REDACTED,
                        "postcode": _REDACTED,
                        "eu_office_postcode": _REDACTED,
                        "members_info": _REDACTED,
                        # A list overwrites the old one only when it is not
                        # empty (the sink drops empty lists).
                        **{k: [_REDACTED] for k in _NAMED_LISTS},
                        "active": False,
                        "deregistered_at": deregistered_at,
                    },
                ),
            )
            n += 1
    return n


def download_register() -> bytes:
    logger.info("Downloading EU Transparency Register XML from %s ...", TR_XML_URL)
    with httpx.Client(timeout=300.0, follow_redirects=True, headers=HTTP_HEADERS) as client:
        resp = client.get(TR_XML_URL)
        resp.raise_for_status()
    logger.info("Downloaded %d MB", len(resp.content) // (1024 * 1024))
    return resp.content


def write_artifact(xml_bytes: bytes, data_dir: str) -> str:
    """The day's register, gzipped, with a sha256 manifest next to it."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, f"tr-{datetime.date.today().isoformat()}.xml.gz")
    with gzip.open(path, "wb") as fh:
        fh.write(xml_bytes)
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    with open(path.replace(".xml.gz", ".manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"file": os.path.basename(path), "sha256": digest,
                   "bytes": len(xml_bytes)}, fh)
    return path


def load_eu_lobbying(log: EventLog, xml_bytes: bytes) -> dict:
    """Emit the register: one UpsertDisclosure per registrant, linked to a
    company where the resolver is confident, and a tombstone for each one
    that has left it."""
    info, entities = parse_register(xml_bytes)
    logger.info("Export date: %s; %d registrants parsed (%s announced)",
                info["export_date"], len(entities), info["registrants"])

    matches, rep_summary = resolve_lobbyist_companies(entities)
    emitted = emit_lobbyist_disclosures(log, entities, matches)
    logger.info(
        "Emitted %d UpsertDisclosure events (%d FILED_BY a resolved company); "
        "resolver: %d confident, %d ambiguous, %d no_match",
        emitted, len(matches), rep_summary["confident"],
        rep_summary["ambiguous"], rep_summary["no_match"],
    )

    # Deregistration: anything we emitted before but that's absent from
    # today's register has dropped off — tombstone it (keep history,
    # redact names).
    current_ids = {e["tr_id"] for e in entities if e.get("tr_id")}
    dropped = _prior_disclosure_ids(os.environ.get("EVENTS_DATABASE_URL")) - current_ids
    deregistered = emit_deregistrations(
        log, dropped, datetime.date.today().isoformat(),
    )
    if deregistered:
        logger.info(
            "Tombstoned %d deregistered lobbyists (names redacted, history kept)",
            deregistered,
        )
    return {
        "emitted": emitted, "represents": rep_summary,
        "deregistered": deregistered,
    }


def main(argv=None) -> None:
    # _run_wrapper always passes argv as a positional, so a bare main()
    # signature blew up the cronjob path with "TypeError: main() takes
    # 0 positional arguments but 1 was given". Every other loader in
    # src/etl/load_*.py uses the same `main(argv=None)` shape.
    parser = argparse.ArgumentParser(
        description="Emit EU Transparency Register events into the event log",
    )
    parser.add_argument("--file", help="Replay a snapshot (tr-YYYY-MM-DD.xml.gz)")
    args = parser.parse_args(argv)
    if args.file:
        with gzip.open(args.file, "rb") as fh:
            xml_bytes = fh.read()
        logger.info("Replaying snapshot %s", args.file)
    else:
        xml_bytes = download_register()
        data_dir = os.environ.get("LOBBYING_DATA_DIR", "/edgar-data/lobbying")
        logger.info("Snapshot written: %s", write_artifact(xml_bytes, data_dir))
    log = EventLog.from_env()
    try:
        load_eu_lobbying(log, xml_bytes)
    finally:
        log.close()


if __name__ == "__main__":
    main()
