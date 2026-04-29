"""Bundled dataset definitions.

The catalog table (`fontem_stats.dataset`) is the runtime source of
truth, but bootstrapping a fresh database needs *something* to insert
the first time. This module is that something: a curated 26-dataset
seed grouped by theme, mirroring the analysis in
docs/eurostat-stats-store.md.
"""
from __future__ import annotations

from ..db import Dataset

EUROSTAT_BULK = (
    "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{code}/"
    "?format=TSV&compressed=true"
)


def _ds(
    code: str,
    label: str,
    theme: str,
    nuts_levels: list[int],
    update_freq: str = "1 year",
    time_unit: str = "year",
    notes: str | None = None,
) -> Dataset:
    return Dataset(
        code=code,
        label=label,
        theme=theme,
        source="eurostat",
        source_url=EUROSTAT_BULK.format(code=code.upper()),
        nuts_levels=nuts_levels,
        # dim_ids/dim_sizes filled in at sync time from upstream metadata;
        # the seed leaves them empty and the first successful sync writes
        # the actual structure back.
        dim_ids=[],
        dim_sizes=[],
        time_unit=time_unit,
        update_freq=update_freq,
        enabled=True,
        notes=notes,
    )


# Curated set of 26 NUTS-keyed numeric datasets — see docs/eurostat-
# stats-store.md for the rationale + sizing.
SEED_DATASETS: list[Dataset] = [
    # ── Population & demography (NUTS-3) ────────────────────────
    _ds("demo_r_pjangrp3", "Population × age × sex × NUTS-3",
        "population", [2, 3]),
    _ds("demo_r_pjanaggr3", "Population aggregates × NUTS-3 (broad age)",
        "population", [2, 3]),
    _ds("demo_r_d3dens", "Population density × NUTS-3",
        "population", [2, 3]),
    _ds("demo_r_gind3", "Demographic balance + crude rates × NUTS-3",
        "population", [2, 3]),
    _ds("demo_r_births", "Live births × NUTS-3",
        "population", [2, 3]),
    _ds("demo_r_magec3", "Deaths × age × sex × NUTS-3",
        "population", [2, 3]),
    _ds("demo_r_mwk3_t", "Weekly deaths × NUTS-3 (excess-mortality data)",
        "population", [2, 3], update_freq="1 week", time_unit="week"),
    _ds("demo_r_minfind", "Infant mortality × NUTS-2",
        "population", [2]),

    # ── Life expectancy & health (NUTS-2) ───────────────────────
    _ds("demo_r_mlifexp", "Life expectancy × age × sex × NUTS-2",
        "health", [2]),
    _ds("hlth_cd_acdr2", "Causes of death × NUTS-2 (3-y averages)",
        "health", [2], notes="3-year rolling average"),
    _ds("hlth_rs_bdsrg", "Hospital beds × NUTS-2 (frozen 2016)",
        "health", [2], notes="historical only — series ends 2016"),

    # ── Economy (NUTS-2 + NUTS-3) ───────────────────────────────
    _ds("nama_10r_3gdp", "GDP × NUTS-3",
        "economy", [2, 3]),
    _ds("nama_10r_3gva", "Gross value added × NUTS-3",
        "economy", [2, 3]),
    _ds("nama_10r_3popgdp", "Population for regional GDP × NUTS-3",
        "economy", [2, 3]),
    _ds("nama_10r_2gdp", "GDP × NUTS-2",
        "economy", [2]),
    _ds("nama_10r_2hhinc", "Household disposable income × NUTS-2",
        "economy", [2]),

    # ── Labour market (NUTS-2) ──────────────────────────────────
    _ds("lfst_r_lfu3rt", "Unemployment rate × education × NUTS-2",
        "labour", [2]),
    _ds("lfst_r_lfp2act", "Labour force × NUTS-2",
        "labour", [2]),
    _ds("lfst_r_lfe2en2", "Employment × NACE × NUTS-2",
        "labour", [2]),

    # ── Education & R&D (NUTS-2) ────────────────────────────────
    _ds("edat_lfse_04", "Population by educational attainment × NUTS-2",
        "education", [2]),
    _ds("rd_e_gerdreg", "R&D expenditure (% GDP) × NUTS-2",
        "rd", [2]),
    _ds("rd_p_persreg", "R&D personnel × NUTS-2",
        "rd", [2]),
    _ds("htec_emp_reg2", "High-tech employment × NUTS-2",
        "rd", [2]),

    # ── Social, mobility, geometry ──────────────────────────────
    _ds("ilc_li41", "At-risk-of-poverty rate × NUTS-2",
        "social", [2]),
    _ds("isoc_r_iuse_i", "Internet users × NUTS-2",
        "digital", [2]),
    _ds("tour_occ_nin2", "Nights at tourist accommodation × NUTS-3",
        "tourism", [2, 3]),
    _ds("tran_r_vehst", "Stock of vehicles × NUTS-2",
        "transport", [2]),
    _ds("reg_area3", "NUTS-3 area km² (one-off)",
        "geometry", [2, 3], update_freq="10 years",
        notes="static; updates only at NUTS revisions"),

    # ── Migration (NUTS-0) ──────────────────────────────────────
    # Eurostat migration data is country-level only; sub-national
    # detail isn't published in this dataset family. We pin to [0]
    # so the Atlas picker constrains the level toggle accordingly.
    # `migr_asyappctzm` is the monthly applicant series — by far the
    # most timely public European migration data, ~6-week lag.
    _ds("migr_imm1ctz", "Immigration × age × sex × citizenship",
        "migration", [0]),
    _ds("migr_imm8", "Immigration × broad group of citizenship",
        "migration", [0]),
    _ds("migr_emi1ctz", "Emigration × age × sex × citizenship",
        "migration", [0]),
    _ds("migr_acq", "Acquisitions of citizenship × former citizenship",
        "migration", [0]),
    _ds("migr_pop1ctz", "Population on 1 Jan × citizenship",
        "migration", [0]),
    _ds("migr_pop3ctb", "Population × citizenship × country of birth",
        "migration", [0]),
    _ds("migr_asyappctzm", "Asylum applicants (monthly) × citizenship",
        "migration", [0], update_freq="1 month", time_unit="month"),
    _ds("migr_asydcfsta", "First-instance asylum decisions × citizenship",
        "migration", [0]),

    # ── Crime + justice (NUTS-0) ────────────────────────────────
    # Be cautious comparing across countries — recording rules and
    # offence definitions vary. Eurostat publishes the figures; the
    # platform should surface the methodology footnote alongside.
    _ds("crim_off_cat", "Recorded offences by offence category",
        "crime", [0],
        notes="reporting differs by country; use as a within-country trend"),
    _ds("crim_hom_vrel", "Homicide victims × relationship to perpetrator",
        "crime", [0]),
    _ds("crim_pris_pop", "Prison population × sex",
        "crime", [0]),
    _ds("crim_just_age", "Persons in criminal justice × age",
        "crime", [0]),
]


def find(code: str) -> Dataset | None:
    for d in SEED_DATASETS:
        if d.code == code:
            return d
    return None
