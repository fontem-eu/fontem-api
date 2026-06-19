"""
EU Listings + ESEF Financials → events.entity_events
=========================================================
Reads ``eu_entities.json`` and ``summaries/*.json`` from the ESEF
data directory and emits events into the canonical log:

  * ``eu_entities.json`` → UpsertCompany (incremental upsert) plus
    UpsertListing per ticker (LISTED_AS edge materialised by sinks).
  * ``summaries/*.json`` → one UpsertFiling per (company, year).
    Idempotent on the deterministic Filing IRI; no Begin/End.

The two phases share one event-log batch so the run is atomic in
the log.

The ESEF path used to bracket itself with Begin/EndGraphReplace
against ``financials/esef`` for PUT-replace semantics. That pattern
turned out to be unsafe in prod (Neo4j sink OOMed accumulating the
whole bracket on 2026-06-06, also EDGAR + ESEF share the
FinancialYear label so a bracket flush on one wipes the other). We
emit incremental upserts now; stale retracted filings can be
cleaned up by a follow-up sweep if it ever becomes a problem.

Usage:
    python -m src.etl.load_eu_listings --esef-dir /esef-data/esef
"""
from __future__ import annotations

import argparse
import json
import datetime
import logging
import os
import time
import uuid
from pathlib import Path

from fontem_event_schemas import builders
from fontem_events import EventLog

from . import gmr_id

logger = logging.getLogger(__name__)

ESEF_FINANCIALS_GRAPH = "http://data.fontem.eu/graph/financials/esef"
SOURCE = "esef"


def _plausible_filing_year(year: int) -> bool:
    """Annual filings cannot report a future fiscal year. Guards against
    the occasional botched XBRL period-end (2039 / 2113 seen in the wild).
    Mirrors the data-quality assertion values.financialyear_year_range.
    """
    return 1990 <= year <= datetime.date.today().year + 1

# Simplified country → currency mapping for major EU/EEA markets
COUNTRY_CURRENCY = {
    "AT": "EUR", "BE": "EUR", "CY": "EUR", "DE": "EUR",
    "EE": "EUR", "ES": "EUR", "FI": "EUR", "FR": "EUR",
    "GR": "EUR", "IE": "EUR", "IT": "EUR", "LT": "EUR",
    "LU": "EUR", "LV": "EUR", "MT": "EUR", "NL": "EUR",
    "PT": "EUR", "SI": "EUR", "SK": "EUR", "HR": "EUR",
    "GB": "GBP", "CH": "CHF", "SE": "SEK", "DK": "DKK",
    "NO": "NOK", "PL": "PLN", "CZ": "CZK", "HU": "HUF",
    "RO": "RON", "BG": "BGN", "IS": "ISK", "UA": "UAH",
    "LI": "CHF",
}

_FILING_FIELDS = (
    "filing_date", "revenue", "gross_profit",
    "operating_income", "net_income", "eps",
    "total_assets", "equity", "cash_and_equivalents",
    "capex", "operating_cashflow", "free_cashflow",
    "total_liabilities", "current_assets",
    "current_liabilities", "inventory",
    "shares_outstanding", "long_term_debt",
    "interest_expense", "income_tax_expense",
    "depreciation_amortization",
)


def _gmr_id_for_entity(meta: dict, fallback_key: str) -> str:
    """LEI → gmr_id when present, else name+country fallback.
    Same rule the Neo4j-era loader used so the gmr_id is stable
    across re-runs."""
    lei = meta.get("lei", "")
    if len(lei) == 20:
        return str(gmr_id.from_lei(lei))
    return str(gmr_id.from_name(
        meta.get("country", "XX"),
        meta.get("name", fallback_key),
    ))


def emit_listings(emit, entities: dict) -> tuple[int, int]:
    """Emit UpsertCompany for every entity and UpsertListing for
    every entity that carries a ticker. Returns (companies, listings)."""
    companies = 0
    listings = 0
    for key, meta in entities.items():
        gid = _gmr_id_for_entity(meta, key)
        lei = meta.get("lei", "")
        emit.upsert(
            "UpsertCompany",
            iri=f"http://data.fontem.eu/id/Company/{gid}",
            domain="company",
            payload=builders.upsert_company(
                gmr_id=gid,
                lei=lei if len(lei) == 20 else None,
                name=meta.get("name") or None,
                country=meta.get("country") or None,
                active=True,
            ),
        )
        companies += 1

        ticker = meta.get("ticker")
        isin = meta.get("isin")
        # Require BOTH ticker and ISIN. Skipping ISIN-less rows lets
        # the OpenFIGI ``lei`` mode pick the company up later via its
        # bulk-file path (which always carries ISIN) instead of us
        # emitting a suspect Listing here that the consolidator's
        # lei-reeval pass has to retire on every cron run. The
        # ``Company`` row is still emitted above so the consolidator
        # has a target to attach a future Listing to; only the
        # ISIN-less Listing is skipped.
        if not ticker or not isin:
            continue
        emit.upsert(
            "UpsertListing",
            iri=f"http://data.fontem.eu/id/Listing/{ticker}",
            domain="listing",
            payload=builders.upsert_listing(
                ticker=str(ticker),
                company_gmr_id=gid,
                exchange=meta.get("exchange") or None,
                isin=isin,
                currency=COUNTRY_CURRENCY.get(meta.get("country") or "", "EUR"),
                active=True,
            ),
        )
        listings += 1
    return companies, listings


def emit_financials(emit, summaries_dir: Path) -> int:
    """Emit one UpsertFiling event per (company, fiscal year) found
    in ``summaries_dir``. Returns the total filing events emitted.

    Each event is independently MERGEable in the sinks. The Filing
    IRI is deterministic on (gmr_id, year, source), so re-runs are
    idempotent.
    """
    if not summaries_dir.exists():
        logger.warning("Summaries dir not found: %s", summaries_dir)
        return 0

    summaries = sorted(summaries_dir.glob("*.json"))
    docs: list[dict] = []
    for path in summaries:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping %s: %s", path.name, exc)
            continue
        lei = doc.get("lei")
        if not lei or len(lei) != 20:
            continue
        docs.append(doc)

    filings = 0
    files_processed = 0
    for doc in docs:
        company_gmr = str(gmr_id.from_lei(doc["lei"]))
        for filing in doc.get("filings", []):
            year = filing.get("year")
            if year is None:
                continue
            year_int = int(year)
            if not _plausible_filing_year(year_int):
                continue
            extras = {
                k: filing.get(k) for k in _FILING_FIELDS
                if filing.get(k) is not None
            }
            emit.upsert(
                "UpsertFiling",
                iri=(
                    f"http://data.fontem.eu/id/Filing/"
                    f"{_filing_uuid(company_gmr, year_int, SOURCE)}"
                ),
                domain="financials/esef",
                payload=builders.upsert_filing(
                    gmr_id=company_gmr,
                    year=year_int,
                    source=SOURCE,
                    **extras,
                ),
            )
            filings += 1
        files_processed += 1
        if files_processed % 1000 == 0:
            logger.info(
                "  %d files, %d filings",
                files_processed, filings,
            )

    logger.info(
        "ESEF: %d filings emitted from %d files (%d entities had "
        "no LEI / unparseable summary)",
        filings, files_processed, len(summaries) - len(docs),
    )
    return filings


def _filing_uuid(gmr_id_str: str, year: int, source: str) -> uuid.UUID:
    """Deterministic Filing IRI key — must match the sink renderers
    so re-runs land on the same IRI."""
    seed = f"filing:{gmr_id_str}:{year}:{source}"
    return uuid.uuid5(
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), seed,
    )


def load_eu_listings(log: EventLog, esef_dir: Path) -> dict:
    """Single batch covering listings + ESEF financials."""
    entities_path = esef_dir / "eu_entities.json"
    if not entities_path.exists():
        logger.error("eu_entities.json not found at %s", entities_path)
        return {"companies": 0, "listings": 0, "filings": 0}

    entities = json.loads(entities_path.read_text(encoding="utf-8"))
    logger.info("Loaded %d EU entities from %s", len(entities), entities_path)

    summaries_dir = esef_dir / "summaries"
    batch_id = uuid.uuid4()
    t0 = time.time()

    with log.batch(batch_id, producer="load_eu_listings") as emit:
        companies, listings = emit_listings(emit, entities)
        filings = emit_financials(emit, summaries_dir)

    elapsed = time.time() - t0
    logger.info(
        "Done: %d companies, %d listings, %d filings in %.1fs",
        companies, listings, filings, elapsed,
    )
    return {
        "companies": companies, "listings": listings, "filings": filings,
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit EU listings + ESEF financials events",
    )
    parser.add_argument(
        "--esef-dir",
        default=os.environ.get("GMR_ESEF_DATA_DIR", "/esef-data/esef"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    log = EventLog.from_env()
    try:
        load_eu_listings(log, Path(args.esef_dir))
    finally:
        log.close()


if __name__ == "__main__":
    main()
