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
import logging
import os
import uuid
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog

from src.etl._hooks import resolve_entity
from src.etl._http import HTTP_HEADERS

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TR_XML_URL = "https://transparency-register.europa.eu/odplastorganisationxml_en"
EMIT_CHUNK = 500

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


def _parse_entity(elem: ET.Element) -> dict[str, Any]:
    """Parse an interestRepresentative XML element into a flat dict."""
    tr_id = _text(elem, "identificationCode")
    name_el = elem.find("name")
    name = _text(name_el, "originalName") if name_el is not None else ""

    head_office = elem.find("headOffice")
    country = _text(head_office, "country") if head_office is not None else ""
    city = _text(head_office, "city") if head_office is not None else ""

    cost_min = 0
    cost_max = 0
    fin = elem.find("financialData")
    if fin is not None:
        closed = fin.find("closedYear")
        if closed is not None:
            costs = closed.find("costs")
            if costs is not None:
                range_el = costs.find("range")
                if range_el is not None:
                    try:
                        cost_max = int(_text(range_el, "max") or "0")
                    except ValueError:
                        pass
                    try:
                        cost_min = int(_text(range_el, "min") or "0")
                    except ValueError:
                        pass

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


def _company_iri(gmr_id: str) -> str:
    return f"http://data.fontem.eu/id/Company/{gmr_id}"


def emit_lobbyist_disclosures(log: EventLog, entities: list[dict]) -> int:
    """Emit one UpsertDisclosure per lobbyist. company_gmr_id is
    omitted — the registrant is the Lobbyist itself; REPRESENTS
    edges to companies are emitted separately as relationships."""
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
                    "cost_min", "cost_max",
                    "registration_date", "last_updated",
                ):
                    v = ent.get(k)
                    if v not in (None, "", 0, 0.0):
                        details[k] = v
                if ent.get("interests"):
                    details["interests"] = ent["interests"]
                emit.upsert(
                    "UpsertDisclosure",
                    iri=_disclosure_iri(ent["tr_id"]),
                    domain="eu_lobbying",
                    payload=builders.upsert_disclosure(
                        system="eu-lobbying",
                        disclosure_id=ent["tr_id"],
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


def emit_represents_relationships(
    log: EventLog, entities: list[dict],
) -> dict:
    """For each lobbyist, POST /resolve with name + country and
    emit a UpsertRelationship event for confident matches."""
    confident = 0
    ambiguous = 0
    no_match = 0
    chunk: list[tuple[str, str, str, float, str]] = []

    def _flush(buf):
        if not buf:
            return
        with log.batch(uuid.uuid4(), producer="load_eu_lobbying") as emit:
            for tr_id, gmr_id, tier, conf, _ in buf:
                emit.upsert(
                    "UpsertRelationship",
                    iri=_disclosure_iri(tr_id),
                    domain="eu_lobbying",
                    payload=builders.upsert_relationship(
                        src_iri=_disclosure_iri(tr_id),
                        dst_iri=_company_iri(gmr_id),
                        predicate="represents",
                        properties={
                            "tier": tier,
                            "confidence": float(conf),
                        },
                    ),
                )

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
            chunk.append((
                ent["tr_id"], res.match.gmr_id,
                res.match.tier, res.match.confidence, ent["name"],
            ))
            confident += 1
            if len(chunk) >= EMIT_CHUNK:
                _flush(chunk)
                chunk = []
        elif res.hint == "ambiguous":
            ambiguous += 1
        else:
            no_match += 1

    _flush(chunk)
    return {
        "confident": confident, "ambiguous": ambiguous, "no_match": no_match,
    }


def load_eu_lobbying(log: EventLog) -> dict:
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

    emitted = emit_lobbyist_disclosures(log, entities)
    logger.info("Emitted %d UpsertDisclosure events", emitted)

    rep_summary = emit_represents_relationships(log, entities)
    logger.info(
        "Resolver: %d confident matches → relationships, "
        "%d ambiguous, %d no_match",
        rep_summary["confident"], rep_summary["ambiguous"],
        rep_summary["no_match"],
    )
    return {"emitted": emitted, "represents": rep_summary}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Emit EU Transparency Register events into the event log",
    )
    args = parser.parse_args()
    log = EventLog.from_env()
    try:
        load_eu_lobbying(log)
    finally:
        log.close()
