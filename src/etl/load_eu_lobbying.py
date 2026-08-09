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

Usage:
    python -m src.etl.load_eu_lobbying
"""

from __future__ import annotations


import argparse
import datetime
import logging
import os
import uuid
import xml.etree.ElementTree as ET
from typing import Any

import httpx
import psycopg
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
    coverage="Self-declared entries in the EU Transparency Register. Registration is not fully mandatory, and figures are as declared, not audited.",
    upstream="EU Transparency Register",
    update_freq="daily",
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

# Country name normalization (TR uses full names, Company nodes use ISO).
_COUNTRY_MAP = {
    "UNITED STATES": "US", "UNITED KINGDOM": "GB", "GERMANY": "DEU",
    "FRANCE": "FRA", "SPAIN": "ESP", "ITALY": "ITA", "NETHERLANDS": "NLD",
    "BELGIUM": "BEL", "SWEDEN": "SWE", "AUSTRIA": "AUT", "DENMARK": "DNK",
    "FINLAND": "FIN", "IRELAND": "IRL", "POLAND": "POL", "PORTUGAL": "PRT",
    "CZECH REPUBLIC": "CZE", "ROMANIA": "ROU", "HUNGARY": "HUN",
    "GREECE": "GRC", "LUXEMBOURG": "LUX", "CROATIA": "HRV", "BULGARIA": "BGR",
    "SLOVAKIA": "SVK", "SLOVENIA": "SVN", "LITHUANIA": "LTU", "LATVIA": "LVA",
    "ESTONIA": "EST", "MALTA": "MLT", "CYPRUS": "CYP", "SWITZERLAND": "CHE",
    "NORWAY": "NOR", "JAPAN": "JPN", "CANADA": "CAN", "AUSTRALIA": "AUS",
    "CHINA": "CHN", "INDIA": "IND", "BRAZIL": "BRA",
}


def _text(elem: ET.Element | None, path: str) -> str:
    """Extract text from an XML element at the given path."""
    if elem is None:
        return ""
    child = elem.find(path)
    return (child.text or "").strip() if child is not None else ""


# One local per XML field extracted from the lobbying record. The fields
# are documented inline next to where each one is read — collapsing them
# into a kwargs dict would erase the column headers.
def _parse_cost_band(elem: ET.Element) -> tuple[int, int]:
    """Closed-year lobbying spend band as ``(cost_min, cost_max)``.

    The EU register doesn't validate the self-reported range, so
    registrants occasionally transpose the bounds (min > max). Keep the
    band well-ordered when both ends are present rather than returning an
    inverted cost_max < cost_min. A missing bound stays 0 (dropped at
    emit time).
    """
    cost_min = 0
    cost_max = 0
    fin = elem.find("financialData")
    closed = fin.find("closedYear") if fin is not None else None
    costs = closed.find("costs") if closed is not None else None
    range_el = costs.find("range") if costs is not None else None
    if range_el is not None:
        try:
            cost_max = int(_text(range_el, "max") or "0")
        except ValueError:
            pass
        try:
            cost_min = int(_text(range_el, "min") or "0")
        except ValueError:
            pass
    if cost_min and cost_max:
        cost_min, cost_max = min(cost_min, cost_max), max(cost_min, cost_max)
    elif cost_min:
        # Open-top bracket (e.g. ">= 10,000,000"): the register reports a
        # lower bound and no upper one. Mirror it as [min, min] so the band
        # is never inverted (cost_max=0 would read as cost_max < cost_min);
        # the lower bound carries the signal.
        cost_max = cost_min
    return cost_min, cost_max


def _parse_entity(elem: ET.Element) -> dict[str, Any]:  # pylint: disable=too-many-locals
    """Parse an interestRepresentative XML element into a flat dict."""
    tr_id = _text(elem, "identificationCode")
    name_el = elem.find("name")
    name = _text(name_el, "originalName") if name_el is not None else ""

    head_office = elem.find("headOffice")
    country = _text(head_office, "country") if head_office is not None else ""
    city = _text(head_office, "city") if head_office is not None else ""

    cost_min, cost_max = _parse_cost_band(elem)

    ep_passes = 0
    try:
        ep_passes = int(_text(elem, "EPAccreditedNumber") or "0")
    except ValueError:
        pass

    members_fte = 0.0
    members_el = elem.find("members")
    if members_el is not None:
        try:
            members_fte = float(_text(members_el, "membersFTE") or "0")
        except ValueError:
            pass

    interests = []
    interests_el = elem.find("interests")
    if interests_el is not None:
        for interest in interests_el.findall("interest"):
            interest_name = _text(interest, "name")
            if interest_name:
                interests.append(interest_name)

    return {
        "tr_id": tr_id,
        "name": name,
        "acronym": _text(elem, "acronym"),
        "country": country,
        "country_iso": _COUNTRY_MAP.get(country.upper(), country),
        "city": city,
        "category": _text(elem, "registrationCategory"),
        "entity_form": _text(elem, "entityForm"),
        "website": _text(elem, "webSiteURL"),
        "goals": (_text(elem, "goals") or "")[:500],
        "ep_passes": ep_passes,
        "members_fte": members_fte,
        "cost_min": cost_min,
        "cost_max": cost_max,
        "registration_date": _text(elem, "registrationDate")[:10],
        "last_updated": _text(elem, "lastUpdateDate")[:10],
        "interests": interests,
    }


def _disclosure_iri(tr_id: str) -> str:
    return f"http://data.fontem.eu/id/EuLobbyingDisclosure/{tr_id}"


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
    """
    matches = matches or {}
    emitted = 0
    chunk: list[dict] = []

    def _flush(buf: list[dict]) -> int:
        if not buf:
            return 0
        n = 0
        with log.batch(uuid.uuid4(), producer="load_eu_lobbying") as emit:
            for ent in buf:
                details: dict[str, object] = {}
                for k in (
                    "name", "acronym", "country", "country_iso",
                    "city", "category", "entity_form", "website",
                    "goals", "ep_passes", "members_fte",
                    "registration_date", "last_updated",
                ):
                    v = ent.get(k)
                    if v not in (None, "", 0, 0.0):
                        details[k] = v
                # Always emit both cost bounds together when either is present,
                # so a shrunk bracket (the lower bound drops out across loads)
                # overwrites a stale cost_min in the sink rather than leaving an
                # inverted cost_max < cost_min. _parse_cost_band already orders
                # a transposed pair; this closes the stale-lower-bound case.
                if ent.get("cost_min") or ent.get("cost_max"):
                    details["cost_min"] = ent.get("cost_min", 0)
                    details["cost_max"] = ent.get("cost_max", 0)
                if ent.get("interests"):
                    details["interests"] = ent["interests"]
                details["active"] = True
                match = matches.get(ent["tr_id"])
                if match is not None:
                    details["registrant_match_tier"] = match[1]
                    details["registrant_match_confidence"] = float(match[2])
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
                        url=ent.get("website") or None,
                        details=details or None,
                    ),
                )
                n += 1
        return n

    for ent in entities:
        if not ent.get("tr_id"):
            continue
        chunk.append(ent)
        if len(chunk) >= EMIT_CHUNK:
            emitted += _flush(chunk)
            chunk = []
    emitted += _flush(chunk)
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
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT iri FROM events.entity_events WHERE producer = %s",
            ("load_eu_lobbying",),
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
    untouched.
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
                        "active": False,
                        "deregistered_at": deregistered_at,
                    },
                ),
            )
            n += 1
    return n


def load_eu_lobbying(log: EventLog) -> dict:  # pylint: disable=too-many-locals
    """Download TR XML and emit Lobbyist/REPRESENTS events."""
    logger.info("Downloading EU Transparency Register XML from %s ...", TR_XML_URL)
    with httpx.Client(timeout=120.0, follow_redirects=True,
                      headers=HTTP_HEADERS) as client:
        resp = client.get(TR_XML_URL)
        resp.raise_for_status()
    xml_bytes = resp.content
    logger.info("Downloaded %d MB", len(xml_bytes) // (1024 * 1024))

    # Clean invalid XML character references before parsing.
    import re  # pylint: disable=import-outside-toplevel
    xml_text = xml_bytes.decode("utf-8", errors="replace")
    xml_text = re.sub(r"&#x[0-9a-fA-F]{1,2};", "", xml_text)
    xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)

    root = ET.fromstring(xml_text)
    meta = root.find("metaData")
    if meta is not None:
        logger.info("Export date: %s, entities: %s",
                    _text(meta, "exportDate"), _text(meta, "numberOfIR"))

    result_list = root.find("resultList")
    if result_list is None:
        logger.error("No resultList found in XML")
        return {"emitted": 0, "represents": {}}

    entities: list[dict] = []
    for elem in result_list:
        tag = elem.tag.split("}")[-1]
        if tag != "interestRepresentative":
            continue
        parsed = _parse_entity(elem)
        if parsed["tr_id"]:
            entities.append(parsed)

    logger.info("Parsed %d lobbyist entities", len(entities))

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
    parser.parse_args(argv)
    log = EventLog.from_env()
    try:
        load_eu_lobbying(log)
    finally:
        log.close()


if __name__ == "__main__":
    main()
