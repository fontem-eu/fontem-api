"""
GLEIF Full Dump → events.entity_events
=========================================
Downloads (or reads a local copy of) the GLEIF Level 1
concatenated ZIP, streams the XML with iterparse, and emits
``UpsertCompany`` events for each LEI record.

Incremental MERGE-style — no Begin/EndGraphReplace bracket. The
GLEIF dump is multi-million records; bulk-replacing the Company
graph would wipe entries from US listings, sanctions, etc. The
sinks treat each event as MERGE-by-gmr_id, so re-runs are
idempotent.

Usage:
    python -m src.etl.load_gleif
    python -m src.etl.load_gleif --file /tmp/gleif-lei2.zip
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time
import uuid
import zipfile
from xml.etree.ElementTree import iterparse

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog

from src.services.location_service import LocationService

from . import gmr_id
from ._http import HTTP_HEADERS

logger = logging.getLogger(__name__)

GLEIF_API = "https://leidata.gleif.org/api/v1/concatenated-files/lei2"
NS = "http://www.gleif.org/data/schema/leidata/2016"

# XPath-style tag helpers using the GLEIF namespace. `_t` is a tiny
# string-formatting helper read on every TAG_* line below; the underscore
# marks it module-private. Pylint reads it as a constant and asks for
# UPPER_CASE, which would just shout in every TAG_* expansion.
_t = f"{{{NS}}}"  # pylint: disable=invalid-name
TAG_RECORD = f"{_t}LEIRecord"
TAG_LEI = f"{_t}LEI"
TAG_ENTITY = f"{_t}Entity"
TAG_LEGAL_NAME = f"{_t}LegalName"
TAG_LEGAL_ADDRESS = f"{_t}LegalAddress"
TAG_COUNTRY = f"{_t}Country"
TAG_POSTAL_CODE = f"{_t}PostalCode"
TAG_LEGAL_FORM = f"{_t}LegalForm"
TAG_ENTITY_LEGAL_FORM_CODE = f"{_t}EntityLegalFormCode"
TAG_OTHER_LEGAL_FORM = f"{_t}OtherLegalForm"
TAG_ENTITY_STATUS = f"{_t}EntityStatus"
TAG_ENTITY_CATEGORY = f"{_t}EntityCategory"
TAG_LEGAL_JURISDICTION = f"{_t}LegalJurisdiction"
TAG_ENTITY_CREATION_DATE = f"{_t}EntityCreationDate"
TAG_REGISTRATION_AUTHORITY = f"{_t}RegistrationAuthority"
TAG_RA_ID = f"{_t}RegistrationAuthorityID"
TAG_RA_ENTITY_ID = f"{_t}RegistrationAuthorityEntityID"
TAG_HQ_ADDRESS = f"{_t}HeadquartersAddress"
TAG_FIRST_ADDRESS_LINE = f"{_t}FirstAddressLine"
TAG_CITY = f"{_t}City"
TAG_REGION = f"{_t}Region"
TAG_OTHER_ENTITY_NAMES = f"{_t}OtherEntityNames"
TAG_OTHER_ENTITY_NAME = f"{_t}OtherEntityName"
TAG_REGISTRATION = f"{_t}Registration"
TAG_REGISTRATION_STATUS = f"{_t}RegistrationStatus"


def resolve_latest_url() -> str:
    """Query the GLEIF API for the latest concatenated file URL."""
    resp = httpx.get(f"{GLEIF_API}?page=0&pageSize=1", timeout=30,
                     headers=HTTP_HEADERS)
    resp.raise_for_status()
    data = resp.json()["data"]
    if not data:
        raise RuntimeError("GLEIF API returned no concatenated files")
    file_id = data[0]["id"]
    return f"{GLEIF_API}/get/{file_id}/zip"


def download_zip(url: str) -> io.BytesIO:
    """Download a ZIP file into memory.

    Plain GET with a bounded total deadline rather than `httpx.stream`:
    the streaming variant's `timeout` is per-chunk inactivity, so a
    trickling upstream (1 byte every ~119s, observed against Eurostat
    on a sibling loader) can hang the run forever. GLEIF's full LEI-CDF
    is a few hundred MB compressed — easily fits in the 4Gi pod limit,
    nothing to gain from streaming.
    """
    logger.info("Downloading %s ...", url)
    resp = httpx.get(url, timeout=300.0, follow_redirects=True,
                     headers=HTTP_HEADERS)
    resp.raise_for_status()
    logger.info("Download complete: %d MB", len(resp.content) // (1024 * 1024))
    return io.BytesIO(resp.content)


def _address_block(el):
    """Extract {address, city, region, country, postal_code} from a
    LegalAddress / HeadquartersAddress element; empty dict if absent.

    GLEIF XML <Country> is ISO 3166-1 alpha-2. Fontem's internal
    convention is alpha-3, so normalise at write time — otherwise
    downstream joins against alpha-3-keyed datasets (NUTSRegion,
    location services, statistics) all miss."""
    if el is None:
        return {}
    return {
        "address": _text(el, TAG_FIRST_ADDRESS_LINE),
        "city": _text(el, TAG_CITY),
        "region": _text(el, TAG_REGION),
        "country": LocationService.to_alpha3(_text(el, TAG_COUNTRY)),
        "postal_code": _text(el, TAG_POSTAL_CODE),
    }


def _gleif_record(entity, elem, lei):
    """Build one GLEIF record dict from an <LEIRecord>'s <Entity>.

    The identity block is stored verbatim from the source — never
    inferred; a field the record omits stays None."""
    legal = _address_block(entity.find(TAG_LEGAL_ADDRESS))
    hq = _address_block(entity.find(TAG_HQ_ADDRESS))

    legal_form_el = entity.find(TAG_LEGAL_FORM)
    legal_form = None
    if legal_form_el is not None:
        legal_form = (_text(legal_form_el, TAG_ENTITY_LEGAL_FORM_CODE)
                      or _text(legal_form_el, TAG_OTHER_LEGAL_FORM))

    ra = entity.find(TAG_REGISTRATION_AUTHORITY)
    reg = elem.find(TAG_REGISTRATION)
    oen = entity.find(TAG_OTHER_ENTITY_NAMES)
    aliases = [n.text.strip()
               for n in (oen.findall(TAG_OTHER_ENTITY_NAME) if oen is not None
                         else [])
               if n.text and n.text.strip()]

    return {
        "lei": lei,
        "name": _text(entity, TAG_LEGAL_NAME) or None,
        "country": legal.get("country") or None,
        "postal_code": legal.get("postal_code") or None,
        "legal_form": legal_form or None,
        "active": _text(entity, TAG_ENTITY_STATUS) == "ACTIVE",
        "entity_kind": _text(entity, TAG_ENTITY_CATEGORY) or None,
        "registered_as": (
            _text(ra, TAG_RA_ENTITY_ID) if ra is not None else None) or None,
        "registered_at": (
            _text(ra, TAG_RA_ID) if ra is not None else None) or None,
        "jurisdiction": _text(entity, TAG_LEGAL_JURISDICTION) or None,
        "registration_status": (
            _text(reg, TAG_REGISTRATION_STATUS) if reg is not None else None)
        or None,
        "entity_creation_date": _text(entity, TAG_ENTITY_CREATION_DATE) or None,
        "address": legal.get("address") or None,
        "city": legal.get("city") or None,
        "region": legal.get("region") or None,
        "hq_address": hq.get("address") or None,
        "hq_city": hq.get("city") or None,
        "hq_region": hq.get("region") or None,
        "hq_country": hq.get("country") or None,
        "hq_postal_code": hq.get("postal_code") or None,
        "aliases": aliases or None,
    }


def parse_gleif_xml(xml_stream):
    """Streaming parser for LEI-CDF v3.1 XML.

    Yields one record dict per <LEIRecord> (see ``_gleif_record`` for the
    key set). Memory-efficient: clears each element after processing."""
    for _event, elem in iterparse(xml_stream, events=("end",)):
        if elem.tag != TAG_RECORD:
            continue
        lei = _text(elem, TAG_LEI)
        entity = elem.find(TAG_ENTITY)
        if entity is None or not lei or len(lei) != 20:
            elem.clear()
            continue
        record = _gleif_record(entity, elem, lei)
        elem.clear()
        yield record


def _text(parent, tag):
    """Get text content of a child element, or None."""
    child = parent.find(tag)
    return child.text.strip() if child is not None and child.text else None


def emit_gleif(log: EventLog, records) -> dict:
    """Emit one ``UpsertCompany`` event per LEI record. MERGE-style
    upsert (no bracket); the GLEIF dump is multi-million rows and a
    PUT-replace would wipe Companies from other sources.

    Returns ``{"total": <events>, "elapsed_s": <seconds>}``."""
    batch_id = uuid.uuid4()
    total = 0
    t0 = time.time()

    with log.batch(batch_id, producer="load_gleif") as emit:
        for rec in records:
            company_gmr_id = str(gmr_id.from_lei(rec["lei"]))
            emit.upsert(
                "UpsertCompany",
                iri=f"http://data.fontem.eu/id/Company/{company_gmr_id}",
                domain="company",
                payload=builders.upsert_company(
                    gmr_id=company_gmr_id,
                    lei=rec["lei"],
                    name=rec.get("name"),
                    country=rec.get("country"),
                    legal_form=rec.get("legal_form"),
                    postal_code=rec.get("postal_code"),
                    active=rec.get("active"),
                    identity={k: rec.get(k)
                              for k in builders.COMPANY_IDENTITY_FIELDS},
                ),
            )
            total += 1
            if total % 50000 == 0:
                elapsed = time.time() - t0
                rate = total / elapsed if elapsed else 0
                logger.info("  %d companies emitted (%.0f/s)", total, rate)

    elapsed = time.time() - t0
    logger.info("Done: %d companies in %.1fs", total, elapsed)
    return {"total": total, "elapsed_s": round(elapsed, 1)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertCompany events for the GLEIF dump",
    )
    parser.add_argument(
        "--file", help="Path to a local GLEIF ZIP file",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.file:
        logger.info("Reading local file: %s", args.file)
        zf_src = args.file
    else:
        url = resolve_latest_url()
        zf_src = download_zip(url)

    with zipfile.ZipFile(zf_src) as zf:
        xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
        if not xml_names:
            logger.error("No XML file found in ZIP")
            sys.exit(1)

        xml_name = xml_names[0]
        logger.info("Parsing %s ...", xml_name)

        log = EventLog.from_env()
        try:
            with zf.open(xml_name) as xml_stream:
                records = parse_gleif_xml(xml_stream)
                emit_gleif(log, records)
        finally:
            log.close()


if __name__ == "__main__":
    main()
