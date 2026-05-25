"""Eurostat postal-code → NUTS-3 lookup.

Read the vendored ``data/nuts/PCODE_2025_NUTS-2024_v2.0.zip`` and
return a ``{(country_alpha2, normalised_postcode): nuts3_code}``
dict the entity-linker can join Company/Authority/Lobbyist nodes
on. The CSV uses ``NUTS3;CODE`` with both fields quoted in
single quotes (e.g. ``'NL366';'3204 XD'``); the country prefix
of the NUTS3 code is the alpha-2 needed for matching against an
entity's ``postal_code``.

Normalisation strips every non-alphanumeric and uppercases the
result. That makes country-specific quirks (``"3204 XD"`` → ``3204XD``,
``"3660-322"`` → ``3660322``, ``"569 55"`` → ``56955``) join cleanly
regardless of how the upstream producer happened to format the
input. We trade off matching ``"30-1"`` against ``"301"`` for
robustness; both should be vanishingly rare in real postal data.

The mapping is treated as opaque static reference data — no
event-log emission, no ``:Pcode`` nodes — so a future NUTS
revision is just dropping a new zip in ``data/nuts/`` and
rebuilding the image. Bulk-emitting 830k+ events for a join
table that's only useful inside this one linker would be
backend noise we'd then need to scrub on every NUTS bump.
"""
from __future__ import annotations

import csv
import io
import logging
import pathlib
import zipfile

logger = logging.getLogger(__name__)

VENDORED_PCODE_ZIP = (
    pathlib.Path(__file__).resolve().parents[2]
    / "data" / "nuts" / "PCODE_2025_NUTS-2024_v2.0.zip"
)


def normalise(code: str) -> str:
    """Strip non-alphanumeric, uppercase."""
    if not code:
        return ""
    return "".join(c for c in code.upper() if c.isalnum())


def load_lookup(path: pathlib.Path | None = None) -> dict[tuple[str, str], str]:
    """Return ``{(country_a2, normalised_postcode): nuts3_code}``.

    ``path`` defaults to the vendored zip. Reads the zip in-place;
    no temp-disk usage. The CSV is opened text-mode through ``zipfile``
    with explicit UTF-8 because the file ships with a BOM that
    ``csv.DictReader`` would otherwise include in the first column
    name.
    """
    src = path or VENDORED_PCODE_ZIP
    lookup: dict[tuple[str, str], str] = {}
    with zipfile.ZipFile(src) as zf:
        csv_name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(csv_name) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.reader(text, delimiter=";")
            header = next(reader, None)
            if header is None or len(header) < 2:
                raise ValueError(f"PCODE CSV {csv_name} has no header row")
            for row in reader:
                if len(row) < 2:
                    continue
                nuts3 = row[0].strip().strip("'")
                postcode = row[1].strip().strip("'")
                if len(nuts3) < 3 or not postcode:
                    continue
                country_a2 = nuts3[:2]
                key = (country_a2, normalise(postcode))
                # First-write-wins on duplicate keys: the CSV is
                # postcode-level granular and a postcode can span
                # multiple NUTS3 administrative areas (border streets).
                # We deliberately pick one rather than emit ambiguity
                # downstream — the alternative is dropping the row,
                # which buys precision in exchange for recall.
                lookup.setdefault(key, nuts3)
    logger.info("loaded %d postcode → NUTS-3 entries from %s",
                len(lookup), src.name)
    return lookup
