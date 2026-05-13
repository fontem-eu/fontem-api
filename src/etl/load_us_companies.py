"""
US Companies & Listings → events.entity_events
==============================================
Reads company_tickers.json from the EDGAR data directory and emits
``UpsertCompany`` + ``UpsertListing`` events for each (company, ticker)
pair. The Virtuoso + Neo4j sinks pick the events up and project them
into their stores.

Listings stay first-class — downstream price/financial fetchers join
through the Listing node, not through a property fan-out on Company.

Usage:
    python -m src.etl.load_us_companies --edgar-dir /edgar-data/full
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import uuid
from pathlib import Path

from fontem_event_schemas import builders
from fontem_events import EventLog

from . import gmr_id

logger = logging.getLogger(__name__)

BATCH_SIZE = 2000


def load_us_companies(log: EventLog, tickers_data: dict) -> int:
    """Emit UpsertCompany + UpsertListing events for each EDGAR ticker.

    Returns the number of (company, listing) pairs emitted.
    """
    batch_id = uuid.uuid4()
    total = 0
    t0 = time.time()

    with log.batch(batch_id, producer="load_us_companies") as emit:
        for _idx, info in tickers_data.items():
            ticker = info.get("ticker", "")
            cik_raw = info.get("cik_str", "")
            if not ticker or not cik_raw:
                continue
            cik = str(cik_raw).zfill(10)
            company_gmr_id = gmr_id.from_cik(cik)
            ticker_upper = ticker.upper()
            name = info.get("title", "").strip() or None

            company_iri = f"http://data.fontem.eu/id/Company/{company_gmr_id}"
            emit.upsert(
                "UpsertCompany",
                iri=company_iri, domain="company",
                payload=builders.upsert_company(
                    gmr_id=company_gmr_id,
                    name=name,
                    country="US",
                    cik=cik,
                    active=True,
                ),
            )

            listing_iri = f"http://data.fontem.eu/id/Listing/{ticker_upper}"
            emit.upsert(
                "UpsertListing",
                iri=listing_iri, domain="listing",
                payload=builders.upsert_listing(
                    ticker=ticker_upper,
                    company_gmr_id=company_gmr_id,
                    exchange="US",
                    currency="USD",
                    active=True,
                ),
            )
            total += 1
            if total % 5000 == 0:
                logger.info("  %d (company, listing) pairs emitted", total)

    elapsed = time.time() - t0
    logger.info("US companies: %d pairs emitted in %.1fs", total, elapsed)
    return total


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertCompany + UpsertListing events for US tickers",
    )
    parser.add_argument(
        "--edgar-dir",
        default=os.environ.get(
            "GMR_EDGAR_LOCAL_DATA_DIR", "/edgar-data/full"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    tickers_path = (
        Path(args.edgar_dir) / "reference" / "company_tickers.json"
    )
    if not tickers_path.exists():
        logger.error(
            "company_tickers.json not found at %s", tickers_path
        )
        return

    data = json.loads(tickers_path.read_text(encoding="utf-8"))
    logger.info("Loaded %d US tickers from %s", len(data), tickers_path)

    log = EventLog.from_env()
    try:
        load_us_companies(log, data)
    finally:
        log.close()


if __name__ == "__main__":
    main()
