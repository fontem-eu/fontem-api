"""
EU Knowledge Graph (Kohesio) → event log
==========================================
Ingests EU cohesion policy projects and beneficiaries from the
Kohesio per-country CSV exports and emits ``UpsertDisclosure``
events keyed by Wikibase QID, with system='eu-cohesion'. Each
project's beneficiary (when known) is captured as the disclosure's
``company_gmr_id`` so the sinks materialise the FILED_BY edge;
otherwise the disclosure stands alone.

Disclosure-shape choice: a cohesion project is a disclosure of
EU funding directed at a beneficiary, with structured details.
Reusing UpsertDisclosure avoids minting a CohesionProject-specific
schema for what is effectively the same shape.

Data source: per-country CSV files from the official Kohesio data
export API.

URL pattern:
  https://kohesio.ec.europa.eu/api/data/object?id=data/projects-2021-2027/latest/{CC}-pp21-27-latest.csv

Usage:
    python -m src.etl.load_eu_knowledge_graph
    python -m src.etl.load_eu_knowledge_graph --countries PT,FR,DE
    python -m src.etl.load_eu_knowledge_graph --file /tmp/PT-pp21-27-latest.csv
    python -m src.etl.load_eu_knowledge_graph --since 2025-09-01
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import time
import uuid

import httpx
from gmr_event_schemas import builders
from gmr_events import EventLog

from src.services.location_service import LocationService

from . import gmr_id

logger = logging.getLogger(__name__)

KOHESIO_CSV_URL = (
    "https://kohesio.ec.europa.eu/api/data/object"
    "?id=data/projects-2021-2027/latest/{cc}-pp21-27-latest.csv"
)

EU_COUNTRIES = [
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES",
    "FI", "FR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT",
    "NL", "PL", "PT", "RO", "SE", "SI", "SK",
]

EMIT_CHUNK = 1000


def _extract_qid(uri: str) -> str:
    """Extract a Wikibase QID from a URI like .../entity/Q123."""
    if not uri:
        return ""
    part = uri.rsplit("/", maxsplit=1)[-1]
    if part.startswith("Q") and part[1:].isdigit():
        return part
    return ""


def _normalize_date(raw: str) -> str:
    """Convert DD/MM/YYYY to YYYY-MM-DD. Returns '' on failure."""
    raw = (raw or "").strip()[:10]
    if not raw:
        return ""
    if len(raw) >= 10 and raw[4] == "-":
        return raw[:10]
    parts = raw.split("/")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return raw


def _to_float(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def download_country_csv(country_code: str) -> bytes:
    """Download a single country's CSV from Kohesio."""
    url = KOHESIO_CSV_URL.format(cc=country_code)
    logger.info("Downloading %s ...", url)
    resp = httpx.get(
        url, timeout=300, follow_redirects=True,
        headers={"User-Agent": "GMR-KnowledgeGraph/1.0"},
    )
    resp.raise_for_status()
    logger.info("  %s: %d KB", country_code, len(resp.content) // 1024)
    return resp.content


def parse_kohesio_csv(data_bytes: bytes, since: str | None = None):
    """Parse a Kohesio CSV and yield project dicts."""
    text = io.StringIO(data_bytes.decode("utf-8", errors="replace"))
    reader = csv.DictReader(text)

    for row in reader:
        start_date = _normalize_date(row.get("Operation_Start_Date", ""))
        end_date = _normalize_date(row.get("Operation_End_Date", ""))

        # --since filter: prefer start_date, fall back to end_date.
        filter_date = start_date or end_date
        if since:
            if not filter_date:
                continue
            if filter_date < since:
                continue

        op_uri = row.get("Operation_Unique_Identifier", "")
        qid = _extract_qid(op_uri)
        if not qid:
            continue

        raw_country = (row.get("CountryCode") or "")[:5].strip()
        country_code = LocationService.to_alpha3(raw_country) or raw_country

        nuts_code = (
            row.get("NUTS3_Code")
            or row.get("NUTS2_Code")
            or row.get("NUTS1_Code")
            or ""
        ).strip()

        beneficiary_gmr_id = None
        ben_uri = row.get("Beneficiary_Unique_Identifier", "")
        ben_qid = _extract_qid(ben_uri)
        if ben_qid:
            beneficiary_gmr_id = str(
                gmr_id.from_name(country_code or "EU", f"kohesio_ben:{ben_qid}")
            )

        # project_id remains in the parsed record as a stable
        # UUID5 derived from the QID — not used by the emit path
        # (which uses qid directly as disclosure_id) but kept for
        # backward-compat with the existing parser tests.
        project_id = str(gmr_id.from_name("EU", f"eukg:{qid}"))

        yield {
            "project_id": project_id,
            "qid": qid,
            "wikibase_qid": qid,
            "title": (
                row.get("Operation_Name_English")
                or row.get("Operation_Name_Programme_Language")
                or ""
            )[:500] or None,
            "description": (
                row.get("Operation_Summary_English")
                or row.get("Operation_Summary_Programme_Language")
                or ""
            )[:2000] or None,
            "total_budget": _to_float(
                row.get("Total_Eligible_Expenditure_amount", "")
            ),
            "eu_contribution": _to_float(
                row.get("Project_EU_Budget", "")
            ),
            "fund": (row.get("Fund_Name") or row.get("Fund_Code") or "")[:200] or None,
            "programme": (row.get("Programme_Name") or "")[:200] or None,
            "start_date": start_date or None,
            "end_date": end_date or None,
            "nuts_code": nuts_code or None,
            "country": country_code or None,
            "beneficiary_gmr_id": beneficiary_gmr_id,
            "beneficiary_qid": ben_qid or None,
        }


def emit_disclosure_events(log: EventLog, records: list[dict]) -> dict:
    """Emit one UpsertDisclosure per project. Chunked into
    EMIT_CHUNK-sized batches so each Postgres transaction stays
    bounded."""
    total = 0
    emitted = 0
    chunk: list[dict] = []

    def _flush(buf: list[dict]) -> int:
        if not buf:
            return 0
        batch_id = uuid.uuid4()
        n = 0
        with log.batch(batch_id, producer="load_eu_knowledge_graph") as emit:
            for rec in buf:
                year = None
                if rec.get("start_date"):
                    try:
                        year = int(rec["start_date"][:4])
                    except ValueError:
                        year = None
                # Capture the structured project fields in details.
                details: dict[str, object] = {}
                for k in (
                    "description", "total_budget", "eu_contribution",
                    "fund", "programme", "start_date", "end_date",
                    "nuts_code", "country", "beneficiary_qid",
                ):
                    v = rec.get(k)
                    if v not in (None, ""):
                        details[k] = v
                emit.upsert(
                    "UpsertDisclosure",
                    iri=(
                        f"http://data.fontem.eu/id/EuCohesionDisclosure/"
                        f"{rec['qid']}"
                    ),
                    domain="eu_cohesion",
                    payload=builders.upsert_disclosure(
                        system="eu-cohesion",
                        disclosure_id=rec["qid"],
                        company_gmr_id=rec.get("beneficiary_gmr_id"),
                        disclosure_type="cohesion-project",
                        year=year,
                        title=rec.get("title"),
                        details=details or None,
                    ),
                )
                n += 1
        return n

    for rec in records:
        total += 1
        chunk.append(rec)
        if len(chunk) >= EMIT_CHUNK:
            emitted += _flush(chunk)
            chunk = []

    emitted += _flush(chunk)
    return {"total": total, "emitted": emitted}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit EU cohesion projects into the event log",
    )
    parser.add_argument("--file", help="Path to a local Kohesio CSV file")
    parser.add_argument(
        "--countries",
        default=",".join(EU_COUNTRIES),
        help="Comma-separated country codes (default: all EU-27)",
    )
    parser.add_argument(
        "--since", default="2025-09-01",
        help="Only ingest projects with start_date >= YYYY-MM-DD",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    all_records: list[dict] = []

    if args.file:
        logger.info("Reading local file: %s", args.file)
        try:
            with open(args.file, "rb") as fh:
                data = fh.read()
        except OSError:
            logger.exception("Failed to read %s", args.file)
            sys.exit(1)
        all_records = list(parse_kohesio_csv(data, since=args.since))
    else:
        countries = [c.strip() for c in args.countries.split(",") if c.strip()]
        logger.info("Downloading %d countries (since=%s)", len(countries), args.since)
        for cc in countries:
            try:
                data = download_country_csv(cc)
                records = list(parse_kohesio_csv(data, since=args.since))
                logger.info("  %s: %d projects after date filter", cc, len(records))
                all_records.extend(records)
            except httpx.HTTPError:
                logger.warning("  %s: download failed, skipping", cc)

    logger.info("Total: %d projects to emit", len(all_records))

    if not all_records:
        logger.info("No records to emit, exiting")
        return

    log = EventLog.from_env()
    t0 = time.time()
    try:
        summary = emit_disclosure_events(log, all_records)
    finally:
        log.close()
    elapsed = time.time() - t0
    logger.info(
        "Done: %d projects, %d events emitted in %.1fs",
        summary["total"], summary["emitted"], elapsed,
    )


if __name__ == "__main__":
    main()
