"""Canonical registry of the platform's data sources.

One row per ETL feed, mapping the three identifiers that live in
different places — the entity_events ``producer``, the etl_run
``cronjob_name``, and the user-facing dashboard route — into a single
addressable source. The data-quality hub and the per-source pipeline
health endpoint both iterate this list so a new feed shows up in the
dashboard the moment it is registered here.

``theme`` is forward-looking: it groups sources for the planned
theme-centric dashboard overhaul. It is advisory today (the hub can
group by it) and does not affect data flow.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DataSource:
    """A single registered ETL feed."""

    id: str        # stable slug; matches the dashboard route segment
    label: str     # human display name
    producer: str  # events.entity_events.producer
    cronjob: str   # events.etl_run.cronjob_name
    theme: str     # grouping for the planned theme-centric overhaul
    route: str     # frontend dashboard path (None-ish feeds omit it)


# Ordered roughly by theme so the hub can render grouped sections without
# its own taxonomy. Producer/cronjob strings are verified against
# events.entity_events.producer and events.etl_run.cronjob_name in prod.
DATA_SOURCES: tuple[DataSource, ...] = (
    # ── Public procurement ──────────────────────────────────────────
    DataSource("contracts", "TED Contracts", "load_ted_contracts",
               "etl-ted-contracts", "procurement", "/data-quality/contracts"),
    # ── Corporate registry / ownership / financials ─────────────────
    DataSource("gleif", "GLEIF Entities", "load_gleif",
               "etl-gleif", "corporate", "/data-quality/gleif"),
    DataSource("gleif-relationships", "GLEIF Relationships",
               "load_gleif_relationships", "etl-gleif-relationships",
               "corporate", "/data-quality/gleif"),
    DataSource("us-companies", "US Companies (EDGAR)", "load_us_companies",
               "etl-us-companies", "corporate", "/data-quality/edgar"),
    DataSource("us-financials", "US Financials (EDGAR)", "load_us_financials",
               "etl-us-financials", "corporate", "/data-quality/edgar"),
    DataSource("eu-listings", "EU Filings (ESEF)", "load_eu_listings",
               "etl-eu-listings", "corporate", "/data-quality/esef"),
    # ── Securities / instruments ────────────────────────────────────
    DataSource("firds", "FIRDS Instruments", "load_firds",
               "etl-firds", "securities", "/data-quality/firds"),
    DataSource("openfigi", "OpenFIGI Enrichment", "load_openfigi",
               "etl-openfigi", "securities", "/data-quality/openfigi"),
    # ── Influence / accountability ──────────────────────────────────
    DataSource("lobbying", "EU Lobbying", "load_eu_lobbying",
               "etl-eu-lobbying", "influence", "/data-quality/lobbying"),
    DataSource("sanctions", "EU Sanctions", "load_eu_sanctions",
               "etl-eu-sanctions", "influence", "/data-quality/sanctions"),
    DataSource("eu-knowledge-graph", "EU Cohesion (Kohesio)",
               "load_eu_knowledge_graph", "etl-eu-knowledge-graph",
               "influence", "/data-quality/eu-knowledge-graph"),
    # ── Geography ───────────────────────────────────────────────────
    DataSource("nuts", "NUTS Regions", "load_nuts",
               "etl-nuts", "geography", "/data-quality/nuts"),
)

# Fast lookups.
BY_ID: dict[str, DataSource] = {s.id: s for s in DATA_SOURCES}
BY_PRODUCER: dict[str, DataSource] = {s.producer: s for s in DATA_SOURCES}
