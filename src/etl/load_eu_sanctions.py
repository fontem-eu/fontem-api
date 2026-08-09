"""
EU Consolidated Financial Sanctions List → Virtuoso
=====================================================
Downloads (or reads a local copy of) the EU consolidated sanctions XML
and writes it to Virtuoso (authoritative store) as SHACL-validated
Turtle, into the ``http://data.fontem.eu/graph/sanctions`` named
graph.

Neo4j SanctionedEntity nodes are projected by the sink. The *auto*
SANCTIONED edge stays retired — it once produced 8/8 false positives
(short acronym names like "LRA"/"AMD" matching unrelated EU companies,
a defamation risk). Instead, for each confident, guarded resolver match
(MIN_NAME_LEN>=6) we mint the sanctioned org as its OWN ``:Company``
(the info we have: best name + country), link that company to the
SanctionedEntity with a ``SANCTIONED`` edge, and emit an
``AssertSameAs`` to the resolved existing company. That same_as is
``:Company`` <-> ``:Company`` — the sink rejects cross-label same_as —
and lands UNREVIEWED: a review candidate a human adjudicates, never an
automatic attribution.

GDPR note: this loader republishes identified-person data (sanctioned
individuals). Natural persons were filtered out until 2026-07-14; they
are now ingested by owner decision, carried as ``subject_type=person``.
The processing lawful basis is Art 6(1)(e) — public interest task
derived from the EU's own publication — but downstream data-subject
rights still attach: rectification (Art 16) and erasure
(Art 17) requests reach Fontem at **gdpr@fontem.eu**. The EU's own
delist process is the upstream source of truth — when an entry
disappears from the FSF feed, the sink tombstones the IRI rather than
silently leaving stale "still sanctioned" assertions in the graph.

Usage:
    python -m src.etl.load_eu_sanctions --file /tmp/sanctions.xml \\
        --virtuoso-sparql-endpoint http://virtuoso.gmr.svc.cluster.local:8890/sparql
"""

from __future__ import annotations


import argparse
import logging
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog

from src.services.location_service import LocationService
from src.etl.data_description import DataDescription

from . import gmr_id
from ._hooks import resolve_entity
from ._http_retry import get_with_retry

DESCRIPTION = DataDescription(
    producer="load_eu_sanctions",
    label="EU Sanctions",
    theme="influence",
    summary="Persons and entities under EU financial sanctions.",
    entities=(
        "SanctionedEntity",
    ),
    coverage="The EU consolidated list. Links from a sanctioned entity to a company in the graph are review candidates, never automatic assertions.",
    upstream="EU Consolidated Financial Sanctions List",
    update_freq="daily",
    answers=(
        "Whether a person or entity is under EU sanctions, and since when",
    ),
)


logger = logging.getLogger(__name__)

SANCTIONS_URL = (
    "https://webgate.ec.europa.eu/fsd/fsf/public/files/"
    # gitleaks only honours `gitleaks:allow` on the same line as the
    # match, and pylint's C0301 fires at 100 chars; keep both happy.
    "xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"  # gitleaks:allow — public EU sanctions portal param, not a credential  # pylint: disable=line-too-long
)

BATCH_SIZE = 500

# Sanction → Company linking is delegated to gmr-consolidator's
# /resolve endpoint. Each SanctionedEntity record carries a primary
# `name` (often a short acronym) and a list of `aliases` (the actual
# multilingual entity names). We try /resolve once with the primary
# name, then fall back to each alias if no confident match. All
# guards (MIN_NAME_LEN=6, country agreement, score floor) live in
# the resolver — a single source of truth for matching.
#
# MIN_NAME_LEN is kept here as a documented invariant (≥6) — it's
# the cap that prevented the historical short-acronym false
# positives. Tests assert it.
MIN_NAME_LEN = 6

NS = "http://eu.europa.ec/fpi/fsd/export"


def _tag(local: str) -> str:
    """Build a namespace-qualified tag name."""
    return f"{{{NS}}}{local}"


def _child_text(elem, local_name: str) -> str:
    """Get text content of a namespace-qualified child element."""
    child = elem.find(_tag(local_name))
    return (child.text or "").strip() if child is not None else ""


def _child_attr(elem, local_name: str, attr: str) -> str:
    """Get an attribute from a namespace-qualified child element."""
    child = elem.find(_tag(local_name))
    if child is not None:
        return (child.attrib.get(attr) or "").strip()
    return ""


def _parse_entity_type(entity_el):
    """Determine whether the sanctioned subject is a person or entity.

    The subjectType element has a ``code`` attribute: 'person' or 'enterprise'.
    """
    subject_type_el = entity_el.find(_tag("subjectType"))
    if subject_type_el is not None:
        raw = (subject_type_el.attrib.get("code") or "").strip().lower()
    else:
        raw = "entity"
    return "person" if "person" in raw else "entity"


def _collect_names(entity_el):
    """Return (primary_name, aliases) from nameAlias child elements.

    Names are stored as attributes on ``nameAlias`` elements:
    ``wholeName``, ``firstName``, ``lastName``.
    """
    names = []
    for alias_el in entity_el.findall(_tag("nameAlias")):
        whole = (alias_el.attrib.get("wholeName") or "").strip()
        if whole and len(whole) > 1:
            names.append(whole)
            continue
        # Fall back to firstName + lastName
        first = (alias_el.attrib.get("firstName") or "").strip()
        last = (alias_el.attrib.get("lastName") or "").strip()
        combined = f"{first} {last}".strip()
        if combined and len(combined) > 1:
            names.append(combined)

    # De-duplicate while preserving order
    seen = set()
    unique = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)

    primary = unique[0] if unique else ""
    aliases = unique[1:] if len(unique) > 1 else []
    return primary, aliases


def _find_nationality(entity_el):
    """Extract the entity's country as alpha-3.

    Persons carry it on ``citizenship``; **enterprises have no
    citizenship and instead carry their country on ``address``
    (1337/1589 of them) and often ``identification``.** Reading only ``citizenship`` left
    every non-person entity country-less, so the country-gated resolver
    matched nothing. Check all three, in that order; the portal stores
    ISO 3166-1 alpha-2, normalise to alpha-3 (descriptions that aren't a
    code pass through as-is, e.g. legacy "UNKNOWN-ish" strings).
    """
    for el_name in ("citizenship", "address", "identification"):
        for el in entity_el.findall(_tag(el_name)):
            country = (el.attrib.get("countryIso2Code") or "").strip()
            if country and country != "00":
                return LocationService.to_alpha3(country) or country
            desc = (el.attrib.get("countryDescription") or "").strip()
            if desc and desc != "UNKNOWN":
                return desc
    return ""


def _find_designation_date(entity_el):
    """Extract the earliest regulation publication date as designation date."""
    earliest = ""
    for reg_el in entity_el.findall(_tag("regulation")):
        pub_date = (reg_el.attrib.get("publicationDate") or "").strip()[:10]
        if pub_date and (not earliest or pub_date < earliest):
            earliest = pub_date
    return earliest


def _find_programme(entity_el):
    """Extract the sanction programme/regime from regulation elements."""
    for reg_el in entity_el.findall(_tag("regulation")):
        prog = (reg_el.attrib.get("programme") or "").strip()
        if prog:
            return prog
    return ""


def parse_sanctions_xml(xml_bytes):
    """Parse EU sanctions XML and yield entity dicts.

    The XML uses namespace ``http://eu.europa.ec/fpi/fsd/export``.
    Each ``sanctionEntity`` element contains nameAlias, subjectType,
    citizenship, regulation, and remark children.
    """
    root = ET.fromstring(xml_bytes)

    for entity_el in root.findall(_tag("sanctionEntity")):
        eu_ref = (
            entity_el.attrib.get("euReferenceNumber")
            or entity_el.attrib.get("logicalId")
            or ""
        ).strip()
        if not eu_ref:
            continue

        entity_type = _parse_entity_type(entity_el)
        primary_name, aliases = _collect_names(entity_el)
        nationality = _find_nationality(entity_el)
        designation_date = _find_designation_date(entity_el)
        regime = _find_programme(entity_el)
        reason = _child_text(entity_el, "remark")

        # Legal basis from the regulation numberTitle attribute
        legal_basis = ""
        reg_el = entity_el.find(_tag("regulation"))
        if reg_el is not None:
            legal_basis = (reg_el.attrib.get("numberTitle") or "").strip()

        entity_id = str(gmr_id.from_name("EU", f"sanction:{eu_ref}"))

        yield {
            "entity_id": entity_id,
            "eu_reference": eu_ref,
            "name": primary_name,
            "entity_type": entity_type,
            "aliases": aliases,
            "nationality": nationality,
            "designation_date": designation_date[:10],
            "sanction_regime": regime[:200],
            "legal_basis": legal_basis[:200],
            "listing_reason": reason[:500],
        }


def _resolve_sanction_to_company(entity: dict) -> dict | None:
    """Try /resolve with the sanction's primary name, then each alias.

    Returns a row dict ready for MERGE_SANCTIONED, or None if no
    confident match was found across name + aliases.

    The EU XML stores the short code in `name` (e.g. "AMD") and the
    real legal entity name in `aliases` (e.g. "Aran Modern Devices").
    Iterating aliases lets us catch real matches that the primary
    name alone would miss; the resolver's MIN_NAME_LEN guard rejects
    each acronym attempt automatically.
    """
    nationality = entity.get("nationality") or ""
    if not nationality:
        return None
    candidates = [entity.get("name") or ""]
    candidates.extend(entity.get("aliases") or [])
    for idx, candidate_name in enumerate(candidates):
        if not candidate_name:
            continue
        result = resolve_entity(
            entity_type="Company",
            name=candidate_name,
            country=nationality,
        )
        if result is None or result.match is None:
            continue
        if result.hint != "matched":
            continue
        return {
            "entity_id": entity["entity_id"],
            "gmr_id": result.match.gmr_id,
            "designation_date": entity["designation_date"],
            "tier": result.match.tier,
            "confidence": result.match.confidence,
            "matched_via_alias": idx > 0,
        }
    return None


def _best_name(entity: dict) -> str:
    """The most complete (longest) of the sanction's names — the full
    legal name, not the short acronym the feed often lists first."""
    names = [n for n in [entity.get("name")] + list(entity.get("aliases") or []) if n]
    return max(names, key=len) if names else ""


def resolve_company_links(entities):
    """Run each non-person entity through the guarded resolver and
    return its confident matches as same_as REVIEW candidates.

    Each returned row is emitted by the caller as an ``AssertSameAs``
    between the SanctionedEntity and the resolved Company, landing as
    an UNREVIEWED ``:SAME_AS`` edge for human adjudication — never an
    automatic SANCTIONED attribution. The resolver's MIN_NAME_LEN>=6
    guard rejects the short-acronym shapes ("LRA", "AMD", …) that
    caused the original 8 false-positive SANCTIONED edges.

    Returns ``(rows, summary)`` — rows to emit, summary for the run log.
    """
    rows = []
    matched_via_alias = 0
    t0 = time.time()
    for entity in entities:
        match = _resolve_sanction_to_company(entity)
        if match is None:
            continue
        rows.append({
            "entity_id": entity["entity_id"],
            "name": _best_name(entity),
            "country": entity.get("nationality") or None,
            "resolved_gmr_id": match["gmr_id"],
            "tier": match["tier"],
            "confidence": match["confidence"],
            "matched_via_alias": match["matched_via_alias"],
        })
        if match["matched_via_alias"]:
            matched_via_alias += 1
    summary = {
        "total": len(entities),
        "matched": len(rows),
        "matched_via_alias": matched_via_alias,
        "no_match": len(entities) - len(rows),
        "elapsed_s": round(time.time() - t0, 1),
    }
    return rows, summary


# main() owns the EU-sanctions ETL state inline: argparse, XML stream open,
# event-log handle, batch counters, error capture, run summary. All loop-
# locals of one sequential pass.
def _emit_review_candidates(log: EventLog, review_rows: list[dict]) -> None:
    """For each guarded resolver match, mint the sanctioned org as its
    own :Company (best name + country), link it to the SanctionedEntity
    with a SANCTIONED edge, and emit a :Company<->:Company AssertSameAs to
    the resolved company — an UNREVIEWED review candidate (the sink sets
    reviewed=false), never an automatic attribution."""
    if not review_rows:
        return
    with log.batch(uuid.uuid4(), producer="load_eu_sanctions") as emit:
        for row in review_rows:
            # Mint the sanctioned org as its OWN :Company, keyed off the
            # sanction id so it never implicitly converges with a same-name
            # company (that silent merge is exactly the FP we must avoid).
            company_gmr_id = str(
                gmr_id.from_name(
                    row["country"] or "EU", f"sanction:{row['entity_id']}"
                )
            )
            company_iri = f"http://data.fontem.eu/id/Company/{company_gmr_id}"
            sanction_iri = (
                "http://data.fontem.eu/id/SanctionedEntity/"
                f"{row['entity_id']}"
            )
            resolved_iri = (
                f"http://data.fontem.eu/id/Company/{row['resolved_gmr_id']}"
            )
            emit.upsert(
                "UpsertCompany", iri=company_iri, domain="sanctions",
                payload=builders.upsert_company(
                    gmr_id=company_gmr_id, name=row["name"], country=row["country"],
                ),
            )
            emit.upsert(
                "UpsertRelationship", iri=company_iri, domain="sanctions",
                payload=builders.upsert_relationship(
                    src_iri=company_iri, dst_iri=sanction_iri, predicate="sanctioned",
                ),
            )
            emit.upsert(
                "AssertSameAs", iri=company_iri, domain="sanctions",
                payload=builders.assert_same_as(
                    a_iri=company_iri, b_iri=resolved_iri,
                    confidence=row["confidence"], method="eu_sanctions_name_country",
                    tier=row["tier"], matched_via_alias=row["matched_via_alias"],
                    rule="sanction_company_review",
                ),
            )


def main(argv=None):  # pylint: disable=too-many-locals
    """CLI entry point.

    The loader emits events into events.entity_events; the
    Virtuoso and Neo4j sinks project them. We no longer write
    either store directly. EVENTS_DATABASE_URL is required.
    """
    parser = argparse.ArgumentParser(
        description="Load EU Consolidated Sanctions List into the event log"
    )
    parser.add_argument("--file", help="Path to local sanctions XML file")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Load XML
    if args.file:
        logger.info("Reading local file: %s", args.file)
        try:
            with open(args.file, "rb") as fh:
                xml_bytes = fh.read()
        except OSError:
            logger.exception("Failed to read file %s", args.file)
            sys.exit(1)
    else:
        logger.info("Downloading sanctions list from %s", SANCTIONS_URL)
        try:
            resp = get_with_retry(SANCTIONS_URL, timeout=120, follow_redirects=True)
            resp.raise_for_status()
            xml_bytes = resp.content
        except httpx.HTTPError:
            logger.exception("Failed to download sanctions list")
            sys.exit(1)

    logger.info("Downloaded/read %d KB", len(xml_bytes) // 1024)
    all_entities = list(parse_sanctions_xml(xml_bytes))
    logger.info("Parsed %d sanctioned entities total", len(all_entities))

    # Persons ingested since 2026-07-14 (owner decision; see GDPR note).
    # The company resolver below still only sees non-person subjects.
    entities = all_entities
    n_persons = sum(1 for e in entities if e["entity_type"] == "person")
    logger.info(
        "Processing %d subjects (%d persons, %d entities)",
        len(entities), n_persons, len(entities) - n_persons,
    )

    log = EventLog.from_env()
    batch_id = uuid.uuid4()
    graph_iri = "http://data.fontem.eu/graph/sanctions"
    written = 0

    with log.batch(batch_id, producer="load_eu_sanctions") as emit:
        emit.control(
            "BeginGraphReplace",
            builders.begin_graph_replace(
                graph_iri=graph_iri,
                label="SanctionedEntity",
                domain="sanctions",
            ),
        )
        for ent in entities:
            iri = (
                "http://data.fontem.eu/id/Sanction/"
                f"{ent['entity_id']}"
            )
            emit.upsert(
                "UpsertSanctionedEntity",
                iri=iri,
                domain="sanctions",
                payload=builders.upsert_sanctioned_entity(
                    entity_id=ent["entity_id"],
                    eu_reference=ent["eu_reference"],
                    subject_type=ent["entity_type"],
                    name=ent.get("name") or None,
                    aliases=ent.get("aliases") or [],
                    nationality=ent.get("nationality") or None,
                    designation_date=(
                        ent.get("designation_date") or None
                    ),
                    sanction_regime=ent.get("sanction_regime") or None,
                    legal_basis=ent.get("legal_basis") or None,
                    listing_reason=ent.get("listing_reason") or None,
                ),
            )
            written += 1
        emit.control(
            "EndGraphReplace",
            builders.end_graph_replace(
                graph_iri=graph_iri, domain="sanctions",
            ),
        )

    logger.info(
        "Emitted %d UpsertSanctionedEntity events (batch_id=%s)",
        written, batch_id,
    )

    # Resolve each entity (guarded); emit confident matches as review
    # candidates (mint sanctioned-org :Company + SANCTIONED edge + a
    # :Company<->:Company AssertSameAs to the resolved company, UNREVIEWED).
    # The auto SANCTIONED edge stays retired (8/8 historical false
    # positives); MIN_NAME_LEN keeps the acronym shapes out of the queue.
    review_rows, summary = resolve_company_links(
        [e for e in entities if e["entity_type"] != "person"]
    )
    _emit_review_candidates(log, review_rows)

    logger.info(
        "Done: %d entities, %d same_as review candidates emitted "
        "(%d via alias), %d no_match in %.1fs",
        summary["total"],
        summary["matched"],
        summary["matched_via_alias"],
        summary["no_match"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
