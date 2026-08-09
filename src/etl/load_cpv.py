"""Load CPV (Common Procurement Vocabulary) reference taxonomy into
the event log as ``UpsertTaxonomyCode`` events.

The complete taxonomy with multilingual labels is committed alongside
this script at ``data/cpv_2008_core.gc.gz``, sourced from the OP-TED
eForms-SDK distribution -- the official OASIS Genericode XML the
Publications Office of the European Union ships for procurement
software. It carries:

  * ~9,500 codes (top-level divisions + every published detail level)
  * Parent/child relationships via the ``parentCode`` column
  * Labels in all 24 EU official languages

The previous incarnation of this loader hand-curated a Python dict of
~115 codes; contracts whose CPV wasn't in that list rendered as a
bare 8-digit number in the UI. This version emits one event per
(code, language), so the full taxonomy materialises across the data
tier and ``graph_contract_source`` resolves every code to a label.

Run modes:

  * default          -- emit every code in every language
  * ``--lang en``    -- emit one ISO 639-1 language only (~9.5K events)
  * ``--download``   -- refresh ``cpv_2008_core.gc.gz`` from OP-TED.
                        Run yearly when the EU publishes an amendment.

Usage::

    python -m src.etl.load_cpv                 # all 24 langs
    python -m src.etl.load_cpv --lang en       # English only
    python -m src.etl.load_cpv --download      # refresh source data
"""

from src.etl.data_description import DataDescription

DESCRIPTION = DataDescription(
    producer="load_cpv",
    label="CPV Vocabulary",
    theme="reference",
    summary="The EU's classification of what public contracts are buying.",
    entities=(
        "CPV",
    ),
    coverage="Reference taxonomy used to categorise tenders by subject.",
    upstream="EU CPV",
    update_freq="one-off",
    answers=(
        "What category of goods or services a contract covers",
    ),
)
from __future__ import annotations

import argparse
import gzip
import logging
import shutil
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog

logger = logging.getLogger(__name__)

SYSTEM = "cpv"

DATA_DIR = Path(__file__).parent / "data"
GC_FILE = DATA_DIR / "cpv_2008_core.gc.gz"
GC_SOURCE_URL = (
    "https://raw.githubusercontent.com/OP-TED/eForms-SDK/main/"
    "codelists/cpv.gc"
)

# Genericode uses ISO 639-3 ("eng", "fra", ...) in the ColumnSet
# ("<lang>_label" per language). Our event schema keys labels by
# ISO 639-1 ("en", "fr", ...) to match the UI's BCP-47 tags. This
# map covers the 24 EU official languages the OP-TED file ships.
ISO3_TO_ISO1 = {
    "bul": "bg", "spa": "es", "ces": "cs", "dan": "da",
    "deu": "de", "est": "et", "ell": "el", "eng": "en",
    "fra": "fr", "gle": "ga", "hrv": "hr", "ita": "it",
    "lav": "lv", "lit": "lt", "hun": "hu", "mlt": "mt",
    "nld": "nl", "pol": "pl", "por": "pt", "ron": "ro",
    "slk": "sk", "slv": "sl", "fin": "fi", "swe": "sv",
}


def _row_values(row) -> dict[str, str]:
    """Pull the ColumnRef -> SimpleValue map out of one Genericode
    <Row>."""
    out: dict[str, str] = {}
    for value in row:
        if not value.tag.endswith("Value"):
            continue
        simple = next(
            (c for c in value if c.tag.endswith("SimpleValue")), None,
        )
        if simple is None or simple.text is None:
            continue
        out[value.attrib.get("ColumnRef", "")] = simple.text
    return out


def _level_from_code(code: str) -> int:
    """Depth of a CPV 8-digit code: how many leading non-zero digits
    run before the trailing zeros. ``45000000`` -> 2 (division
    "45"), ``45200000`` -> 3, ``45234100`` -> 6, ``45234110`` -> 7.
    Used for hierarchical UI grouping; the UpsertTaxonomyCode schema
    already carries this field."""
    trimmed = code.rstrip("0")
    return max(len(trimmed), 1)


def parse_genericode(path: Path):
    """Yield ``(code, parent_code, label_lang, label, level)`` tuples
    for every (code, language) pair in the Genericode file.

    Streams via ``ET.iterparse`` so peak memory stays small on the
    30 MiB-uncompressed input."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        for _event, row in ET.iterparse(fh, events=("end",)):
            if not row.tag.endswith("Row"):
                continue
            vals = _row_values(row)
            code = vals.get("code") or ""
            if not code:
                row.clear()
                continue
            parent = vals.get("parentCode") or None
            level = _level_from_code(code)
            for col, text in vals.items():
                if not col.endswith("_label"):
                    continue
                iso3 = col[:-len("_label")]
                iso1 = ISO3_TO_ISO1.get(iso3)
                if iso1 is None or not text:
                    continue
                yield code, parent, iso1, text, level
            row.clear()


def download_genericode(dest: Path = GC_FILE,
                        url: str = GC_SOURCE_URL) -> int:
    """Fetch the upstream Genericode file and overwrite the committed
    copy. Compressed on-the-fly to keep the repo footprint at ~3 MiB.
    Returns the compressed byte count."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("downloading %s -> %s", url, dest)
    with httpx.stream("GET", url, follow_redirects=True,
                      timeout=60) as resp:
        resp.raise_for_status()
        with gzip.open(dest, "wb") as out:
            for chunk in resp.iter_bytes():
                out.write(chunk)
    size = dest.stat().st_size
    logger.info("wrote %s (%.1f MiB)", dest, size / 1024 / 1024)
    return size


def load_cpv(log: EventLog, *, gc_path: Path = GC_FILE,
             lang: str | None = None) -> int:
    """Emit one UpsertTaxonomyCode event per (code, language) parsed
    from the Genericode file. Idempotent at the (system, code,
    label_lang) level -- re-running upserts the same payloads.

    ``lang`` is an ISO 639-1 filter (e.g. ``"en"``); when None, all
    24 EU languages are emitted."""
    if not gc_path.exists():
        raise FileNotFoundError(
            f"{gc_path} missing. Run with --download to fetch it."
        )

    batch_id = uuid.uuid4()
    total = 0
    t0 = time.time()
    with log.batch(batch_id, producer="load_cpv") as emit:
        for code, parent, label_lang, label, level in parse_genericode(
            gc_path,
        ):
            if lang is not None and label_lang != lang:
                continue
            emit.upsert(
                "UpsertTaxonomyCode",
                iri=f"http://data.fontem.eu/id/Cpv/{code}",
                domain="cpv",
                payload=builders.upsert_taxonomy_code(
                    system=SYSTEM, code=code,
                    label=label, label_lang=label_lang,
                    parent_code=parent, level=level,
                ),
            )
            total += 1
            if total % 25000 == 0:
                logger.info("  %d events emitted", total)
    elapsed = time.time() - t0
    logger.info(
        "Done: %d CPV events in %.1fs (%s langs)",
        total, elapsed, lang or "all 24 EU",
    )
    return total


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertTaxonomyCode events for the CPV catalogue",
    )
    parser.add_argument(
        "--download", action="store_true",
        help="Fetch the upstream Genericode file from OP-TED, "
             "overwrite the committed copy, and exit.",
    )
    parser.add_argument(
        "--lang", default=None,
        help="ISO 639-1 language to emit (default: emit all 24 "
             "EU official languages).",
    )
    parser.add_argument(
        "--gc-file", default=str(GC_FILE),
        help=f"Path to the Genericode source file "
             f"(default: {GC_FILE}).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.download:
        download_genericode(Path(args.gc_file))
        return

    src = Path(args.gc_file)
    if src.resolve() != GC_FILE.resolve():
        logger.info("using non-default source file %s", src)
        shutil.copyfile(src, GC_FILE)
    log = EventLog.from_env()
    try:
        load_cpv(log, gc_path=GC_FILE, lang=args.lang)
    finally:
        log.close()


if __name__ == "__main__":
    main()
