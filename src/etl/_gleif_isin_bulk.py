"""GLEIF ISIN-to-LEI bulk file loader.

GLEIF publishes a daily ``isin-lei-YYYYMMDDTHHMMSS.zip`` containing the
canonical LEI→ISIN relationships for the entire global LEI universe
(~96 000 LEIs that have any ISIN, ~8.8 M rows total at 30 MB zipped /
285 MB CSV). The schema is just two columns::

    LEI,ISIN
    00EHHQ2ZHDCFXJCPCL46,US92204Q1031
    00KLB2PFTM3060S2N216,US4138382027
    …

Cross-checked against the per-LEI REST endpoint
(``api.gleif.org/api/v1/lei-records/{lei}/isins``): same data, same
publish-date metadata. The file is the canonical source — the REST
endpoint serves from it. The REST endpoint is rate-limited per source
IP; the file download is not.

The OpenFIGI loader used to call the per-LEI REST endpoint once per
LEI, which made a 10 000-LEI bulk run trip GLEIF's per-IP throttle
within an hour. This module replaces that loop with a one-shot
download + streaming filter: build a ``dict[lei → list[isin]]`` only
for the LEIs we actually care about (typically ~5 % of the input
cohort have any ISIN at all, so the filtered dict stays small), then
look up locally in O(1) without paying for HTTP per LEI.

Usage::

    target_leis: set[str] = {row["lei"] for row in rows}
    mapping = load_isin_mapping(target_leis)
    for lei in target_leis:
        isins = mapping.get(lei, [])  # was: gleif_get_isins(lei)
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

from ._http import HTTP_HEADERS

logger = logging.getLogger(__name__)

LATEST_URL = "https://mapping.gleif.org/api/v2/isin-lei/latest"
_CACHE_DIR_ENV = "GLEIF_ISIN_CACHE_DIR"

# Pin the GLEIF filename shape so an untrusted ``data.attributes.fileName``
# value can't escape the cache dir via ``../`` or absolute paths.
_FILENAME_RE = re.compile(r"^isin-lei-\d{8}T\d{6}\.zip$")

# Size caps. Today's file is ~30 MB compressed / ~285 MB decompressed;
# 16× / ~7× headroom respectively. Either ceiling aborts with a
# RuntimeError so a compromised upstream can't fill the cache mount or
# OOM the worker.
_DOWNLOAD_MAX_BYTES = 500 * 1024 * 1024
_DECOMPRESSED_MAX_BYTES = 2 * 1024 * 1024 * 1024

csv.field_size_limit(10 * 1024 * 1024)


def _client_for_streaming() -> httpx.Client:
    # trust_env=False bypasses HTTP(S)_PROXY. The GLEIF bulk host
    # (mapping.gleif.org) is a public CDN reachable directly (~0.8s), and
    # it is NOT on the ESMA-proxy allow-list — routing it through that
    # proxy (which openfigi sets for the rate-limited api.gleif.org /
    # api.openfigi.com endpoints) makes the request hang until the 60s
    # read timeout, failing the whole enrichment run. The bulk download
    # doesn't need the proxy's egress-IP rotation; the API calls still get
    # it via their own httpx.get/post (which honour the env).
    return httpx.Client(
        timeout=httpx.Timeout(
            connect=15.0, read=60.0, write=15.0, pool=15.0,
        ),
        headers=HTTP_HEADERS,
        follow_redirects=True,
        trust_env=False,
    )


def _resolve_latest(http_client: httpx.Client) -> tuple[str, str]:
    """Return (filename, download_url) for today's file. Filename is
    validated against ``_FILENAME_RE`` so a tampered or schema-drifted
    upstream can't poison the cache path."""
    resp = http_client.get(LATEST_URL)
    resp.raise_for_status()
    data = resp.json()["data"]
    attrs = data["attributes"]
    filename = attrs["fileName"]
    if not _FILENAME_RE.fullmatch(filename):
        raise RuntimeError(f"untrusted GLEIF fileName: {filename!r}")
    return filename, attrs["downloadLink"]


def _download(
    url: str, out: Path, http_client: httpx.Client,
) -> Path:
    """Stream the bulk zip to ``out``. Atomic write via ``os.replace``
    so concurrent runs overwrite cleanly. Aborts if the running total
    exceeds ``_DOWNLOAD_MAX_BYTES``."""
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_suffix(out.suffix + ".partial")
    if partial.exists():
        partial.unlink()
    logger.info("Downloading GLEIF ISIN-LEI bulk file from %s", url)
    t0 = time.time()
    total = 0
    with http_client.stream("GET", url) as r:
        r.raise_for_status()
        with open(partial, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=256 * 1024):
                total += len(chunk)
                if total > _DOWNLOAD_MAX_BYTES:
                    raise RuntimeError(
                        f"GLEIF bulk download exceeded cap "
                        f"({_DOWNLOAD_MAX_BYTES} bytes)",
                    )
                f.write(chunk)
    os.replace(partial, out)
    logger.info(
        "Downloaded %s (%.0f MB) in %.0fs",
        out, out.stat().st_size / 1e6, time.time() - t0,
    )
    return out


def fetch_latest_zip(
    cache_dir: str | None = None,
    http_client: httpx.Client | None = None,
) -> Path:
    """Return the path to today's bulk zip, downloading if missing.

    **Offline cache fallback.** Before calling ``/latest`` we glob for
    a same-day file already on disk (``isin-lei-YYYYMMDDT*.zip`` with
    today's UTC date). On a hit the local file is returned without
    touching the GLEIF API at all, so a same-day re-run still works
    when /latest is rate-limited or unreachable.
    """
    cache = Path(
        cache_dir
        or os.environ.get(_CACHE_DIR_ENV, "")
        or "/tmp/gleif-isin-bulk"
    )
    cache.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    same_day = sorted(cache.glob(f"isin-lei-{today}T*.zip"))
    if same_day:
        hit = same_day[-1]
        if hit.stat().st_size > 0:
            logger.info("GLEIF ISIN-LEI same-day cache hit: %s", hit)
            return hit

    owns_client = http_client is None
    if owns_client:
        http_client = _client_for_streaming()
    try:
        filename, url = _resolve_latest(http_client)
        out = cache / filename
        if out.exists() and out.stat().st_size > 0:
            logger.info("GLEIF ISIN-LEI cache hit: %s", out)
            return out
        _download(url, out, http_client)
        _prune_old_caches(cache, keep=2)
        return out
    finally:
        if owns_client:
            http_client.close()


def _prune_old_caches(cache: Path, keep: int) -> int:
    """Best-effort: drop everything except the ``keep`` newest
    ``isin-lei-*.zip`` files after a successful download."""
    try:
        files = sorted(
            cache.glob("isin-lei-*.zip"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    except OSError:
        return 0
    pruned = 0
    for path in files[keep:]:
        try:
            path.unlink()
            pruned += 1
        except OSError:
            logger.warning("could not prune stale cache file %s", path)
    if pruned:
        logger.info("Pruned %d stale GLEIF cache file(s)", pruned)
    return pruned


def stream_pairs(zip_path: Path) -> Iterable[tuple[str, str]]:
    """Yield ``(lei, isin)`` tuples by streaming the CSV inside the
    zip. Generator — lets callers filter without materialising all 8 M+
    rows in memory.

    Strict on:
      * exactly one ``*.csv`` entry inside the zip
      * decompressed size bounded by ``_DECOMPRESSED_MAX_BYTES``
      * header equals ``["LEI", "ISIN"]``
      * each row has >= 2 cols (mirrors the strict header contract)
    """
    with zipfile.ZipFile(zip_path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise RuntimeError(
                f"expected exactly one .csv in {zip_path}, "
                f"got {csv_names!r}",
            )
        info = zf.getinfo(csv_names[0])
        if info.file_size > _DECOMPRESSED_MAX_BYTES:
            raise RuntimeError(
                f"GLEIF CSV decompressed size {info.file_size} "
                f"exceeds cap {_DECOMPRESSED_MAX_BYTES}",
            )
        with zf.open(csv_names[0]) as binary:
            text = io.TextIOWrapper(binary, encoding="utf-8", newline="")
            reader = csv.reader(text)
            header = next(reader, None)
            if header != ["LEI", "ISIN"]:
                raise RuntimeError(
                    f"unexpected GLEIF CSV header: {header!r}",
                )
            for row in reader:
                if len(row) < 2:
                    raise RuntimeError(
                        f"malformed GLEIF row (got {len(row)} cols): "
                        f"{row!r}",
                    )
                yield row[0], row[1]


def load_isin_mapping(
    target_leis: set[str] | None = None,
    cache_dir: str | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, list[str]]:
    """Build ``{lei: [isin, ...]}`` for the LEIs we care about.

    ``target_leis=None`` (everything) is exposed for callers that want
    the full mapping. A WARN is logged because that path can peak near
    the container memory limit. ``target_leis=set()`` short-circuits:
    empty cohort, no I/O, ``{}`` returned."""
    if target_leis is not None and not target_leis:
        return {}
    zip_path = fetch_latest_zip(cache_dir, http_client)
    if target_leis is None:
        logger.warning(
            "load_isin_mapping called with target_leis=None — "
            "the full mapping can use up to ~1.5 GB of heap on the "
            "current file size",
        )
    mapping: dict[str, list[str]] = {}
    rows = 0
    for lei, isin in stream_pairs(zip_path):
        rows += 1
        if target_leis is not None and lei not in target_leis:
            continue
        mapping.setdefault(lei, []).append(isin)
    logger.info(
        "GLEIF ISIN-LEI mapping: %d rows streamed, %d LEIs kept "
        "(%d total ISINs)",
        rows, len(mapping), sum(len(v) for v in mapping.values()),
    )
    return mapping


__all__ = [
    "LATEST_URL",
    "fetch_latest_zip",
    "load_isin_mapping",
    "stream_pairs",
]
