"""
EU Consolidated Financial Sanctions List → Virtuoso
=====================================================
Downloads (or reads a local copy of) the EU consolidated sanctions XML
and writes it to Virtuoso (authoritative store) as SHACL-validated
Turtle, into the ``http://data.fontem.eu/graph/sanctions`` named
graph.

Phase 2 cutover is closed: Neo4j SanctionedEntity nodes + SANCTIONED
edges no longer exist, and the loader no longer writes them. The
sanction → company resolver call still runs (the ETL is a candidate
emitter for review queues) but its output is logged-only until the
review-queue refactor that targets Virtuoso lands.

GDPR note: this loader republishes identified-person data (sanctioned
individuals). The processing lawful basis is Art 6(1)(e) — public
interest task derived from the EU's own publication — but downstream
data-subject rights still attach: rectification (Art 16) and erasure
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

from . import gmr_id
from ._hooks import resolve_entity
from ._http_retry import get_with_retry

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
    """Extract nationality from citizenship child elements.

    The ``citizenship`` element has ``countryIso2Code`` and
    ``countryDescription`` attributes.
    """
    for cit_el in entity_el.findall(_tag("citizenship")):
        country = (cit_el.attrib.get("countryIso2Code") or "").strip()
        if country and country != "00":
            return country
        desc = (cit_el.attrib.get("countryDescription") or "").strip()
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


def resolve_company_links(entities):
    """Run each non-person entity through /resolve and return summary.

    The resolver-driven sanction→company linkage was historically
    written as Neo4j SANCTIONED edges. Phase 2 retired that table;
    the linkage will land in Virtuoso once the review-queue path
    moves across. Until then this loop just exercises the resolver
    so we keep observability on its hit rate (logged + returned to
    the CLI) and so any future regression there shows up in the
    daily ETL run.
    """
    matched = 0
    matched_via_alias = 0
    no_match = 0
    t0 = time.time()
    for entity in entities:
        row = _resolve_sanction_to_company(entity)
        if row is None:
            no_match += 1
            continue
        matched += 1
        if row["matched_via_alias"]:
            matched_via_alias += 1
    elapsed = time.time() - t0
    return {
        "total": len(entities),
        "matched": matched,
        "matched_via_alias": matched_via_alias,
        "no_match": no_match,
        "elapsed_s": round(elapsed, 1),
    }


# main() owns the EU-sanctions ETL state inline: argparse, XML stream open,
# event-log handle, batch counters, error capture, run summary. All loop-
# locals of one sequential pass.
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

    # GDPR: skip natural persons — only process non-person entities
    entities = [e for e in all_entities if e["entity_type"] != "person"]
    skipped = len(all_entities) - len(entities)
    logger.info(
        "Filtered to %d non-person entities (skipped %d persons)",
        len(entities), skipped,
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

    # Resolver hit-rate logging only — the SANCTIONED edge to
    # Neo4j was retired in the Phase 2 cutover, and the
    # Virtuoso-side review-queue path lands separately.
    summary = resolve_company_links(entities)

    logger.info(
        "Done: %d entities, %d resolver matches (%d via alias), "
        "%d no_match in %.1fs",
        summary["total"],
        summary["matched"],
        summary["matched_via_alias"],
        summary["no_match"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
