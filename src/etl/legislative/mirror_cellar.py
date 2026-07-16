"""Verbatim CDM mirror of EU legislation from CELLAR into Virtuoso.

Architecture (gitops#290, decided 2026-07-10): CELLAR's metadata is
mirrored in its NATIVE CDM ontology — no vocabulary transformation
anywhere. History and delta are the same job: a date window is exported
via bounded, paged queries; the window's triples are written to a dated,
checksummed artifact on the NFS share FIRST; only then is the artifact
bulk-loaded into the mirror graph over paced HTTP. The endpoint is used
as a scheduled export mechanism producing versioned artifacts — never
ad-hoc harvesting. Depth: work + expression + manifestation (items /
content streams excluded).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

CELLAR_SPARQL = os.environ.get(
    "CELLAR_SPARQL_URL", "https://publications.europa.eu/webapi/rdf/sparql")
MIRROR_GRAPH = "http://data.fontem.eu/graph/mirror/cellar/eu"

# Page size for the work-list SELECT; CELLAR paginates reliably at this
# size without tripping its query cost limits.
WORK_PAGE = 200
# Works per CONSTRUCT closure call (VALUES block size).
CLOSURE_BATCH = 25
# Load chunking: flush on EITHER bound. The byte budget keeps the
# form-encoded INSERT body under Virtuoso's request limits (a 20k-triple
# body 400s); the triple cap keeps transactions small so the store
# checkpoints between chunks (OOM lessons — never one monolithic txn).
LOAD_CHUNK_TRIPLES = 2000
LOAD_CHUNK_BYTES = 1_500_000
LOAD_PAUSE_S = 0.5


def month_windows(start: date, end: date) -> list[tuple[str, str]]:
    """Half-open [first, next-first) ISO date pairs covering start..end
    by calendar month."""
    windows = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        windows.append((f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"))
        y, m = ny, nm
    return windows


def work_list_query(win_start: str, win_end: str, offset: int) -> str:
    """Page of legal-resource work URIs whose document date falls in the
    window. Ordered so pagination is stable."""
    return f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?w WHERE {{
  ?w a cdm:resource_legal ; cdm:work_date_document ?d .
  FILTER(?d >= "{win_start}"^^<http://www.w3.org/2001/XMLSchema#date>
      && ?d <  "{win_end}"^^<http://www.w3.org/2001/XMLSchema#date>)
}} ORDER BY ?w LIMIT {WORK_PAGE} OFFSET {offset}"""


def closure_queries(work_uris: list[str]) -> list[str]:
    """Three CONSTRUCTs per batch — verbatim CDM triples of the works,
    their expressions, and those expressions' manifestations. Three
    separate queries instead of one UNION: a FILTER(?s = ?w) in a UNION
    branch silently yields nothing (?w is unbound inside the branch —
    VALUES joins after branch evaluation), which dropped every
    work-level triple in the first MVP run. The patterns only WALK the
    FRBR chain; every matched triple is emitted exactly as stored."""
    values = " ".join(f"<{u}>" for u in work_uris)
    prefix = "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
    return [
        prefix + f"""CONSTRUCT {{ ?w ?p ?o }}
WHERE {{ VALUES ?w {{ {values} }} ?w ?p ?o }}""",
        prefix + f"""CONSTRUCT {{ ?e ?p ?o }}
WHERE {{ VALUES ?w {{ {values} }}
  ?e cdm:expression_belongs_to_work ?w . ?e ?p ?o }}""",
        prefix + f"""CONSTRUCT {{ ?m ?p ?o }}
WHERE {{ VALUES ?w {{ {values} }}
  ?e cdm:expression_belongs_to_work ?w .
  ?m cdm:manifestation_manifests_expression ?e . ?m ?p ?o }}""",
    ]


def fetch_window(client: httpx.Client, win_start: str, win_end: str):
    """Yield N-Triples lines for one window (paged; bounded calls)."""
    offset = 0
    while True:
        r = client.post(CELLAR_SPARQL,
                        data={"query": work_list_query(win_start, win_end, offset)},
                        headers={"Accept": "application/sparql-results+json"})
        r.raise_for_status()
        works = [b["w"]["value"] for b in r.json()["results"]["bindings"]]
        if not works:
            return
        for i in range(0, len(works), CLOSURE_BATCH):
            for query in closure_queries(works[i:i + CLOSURE_BATCH]):
                rc = client.post(CELLAR_SPARQL, data={"query": query},
                                 headers={"Accept": "application/n-triples"})
                rc.raise_for_status()
                for line in rc.text.splitlines():
                    line = line.strip()
                    # CELLAR emits "# Empty NT" comment lines for
                    # zero-result CONSTRUCTs — legal N-Triples, but a
                    # '#' inside an INSERT DATA body comments out the
                    # rest of the line (it ate the closing braces on
                    # 1949 data). Comments are not triples; drop them.
                    if line and not line.startswith("#"):
                        yield line
        if len(works) < WORK_PAGE:
            return
        offset += WORK_PAGE


def write_artifact(lines, out_dir: Path, window_tag: str) -> dict:
    """Stream triples to a gzip N-Triples artifact + sha256 manifest.
    The artifact is the durable record; loading happens FROM it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"cellar-cdm-{window_tag}.nt.gz"
    sha = hashlib.sha256()
    n = 0
    with gzip.open(path, "wt", encoding="utf-8") as gz:
        for line in lines:
            gz.write(line + "\n")
            sha.update(line.encode() + b"\n")
            n += 1
    manifest = {
        "source": CELLAR_SPARQL, "graph": MIRROR_GRAPH,
        "window": window_tag, "triples": n,
        "sha256_uncompressed": sha.hexdigest(),
        "artifact": path.name, "bytes": path.stat().st_size,
    }
    (out_dir / f"cellar-cdm-{window_tag}.manifest.json").write_text(
        json.dumps(manifest, indent=1))
    return manifest


def load_artifact(client: httpx.Client, update_url: str, path: Path,
                  pause_s: float = LOAD_PAUSE_S) -> int:
    """Paced INSERT of an N-Triples artifact into the mirror graph.
    Chunked so no single transaction can pressure the store; identical
    triples are naturally idempotent in the quad store."""
    loaded = 0
    chunk: list[str] = []
    chunk_bytes = 0

    def flush():
        nonlocal loaded, chunk_bytes
        if not chunk:
            return
        # Virtuoso's /sparql-auth silently prepends `define
        # sql:big-data-const 0`, which 400s on constant-heavy INSERT
        # bodies — override it, same workaround as the virtuoso sink.
        body = ("define sql:big-data-const 1\n"
                "INSERT DATA { GRAPH <" + MIRROR_GRAPH + "> { "
                + "\n".join(chunk) + " } }")
        r = client.post(update_url, data={"query": body})
        if r.status_code >= 400:
            logger.error("load chunk failed (%d): %s",
                         r.status_code, r.text[:300])
        r.raise_for_status()
        loaded += len(chunk)
        chunk.clear()
        chunk_bytes = 0
        time.sleep(pause_s)

    with gzip.open(path, "rt", encoding="utf-8") as gz:
        for line in gz:
            line = line.strip()
            # belt-and-braces: artifacts written before the export-side
            # comment filter may still carry "# Empty NT" lines.
            if line and not line.startswith("#"):
                chunk.append(line)
                chunk_bytes += len(line)
            if len(chunk) >= LOAD_CHUNK_TRIPLES or chunk_bytes >= LOAD_CHUNK_BYTES:
                flush()
    flush()
    return loaded


def _resolve_range(parser, args) -> tuple[date, date]:
    if args.recent:
        today = date.today()
        prev = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        return prev, today.replace(day=1)
    if not (args.date_from and args.date_to):
        parser.error("--from/--to required unless --recent")
    return (date(*[int(x) for x in args.date_from.split("-")], 1),
            date(*[int(x) for x in args.date_to.split("-")], 1))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from",
                        help="window start, YYYY-MM")
    parser.add_argument("--to", dest="date_to",
                        help="window end (inclusive month), YYYY-MM")
    parser.add_argument("--recent", action="store_true",
                        help="delta mode for the daily cron: previous + "
                             "current month (idempotent re-export catches "
                             "late publications and corrigenda)")
    parser.add_argument("--out", default=os.environ.get(
        "LEGISLATIVE_DATA_DIR", "/edgar-data/legislative/cellar"))
    parser.add_argument("--skip-load", action="store_true",
                        help="export artifacts only")
    args = parser.parse_args(argv)
    start, end = _resolve_range(parser, args)
    out_dir = Path(args.out)

    update_url = None
    if not args.skip_load:
        base = os.environ["VIRTUOSO_SPARQL_URL"].rstrip("/").removesuffix("/sparql")
        update_url = f"{base}/sparql-auth"

    totals = {"windows": 0, "triples": 0, "loaded": 0}
    # CELLAR's paged CONSTRUCTs stall past 300s on heavy month windows
    # (first seen: 2007-12 killed the prod walk's year 5x). Generous
    # default, env-tunable for the walk vs the small daily delta.
    cellar_timeout = float(os.environ.get("CELLAR_EXPORT_TIMEOUT", "600"))
    with httpx.Client(timeout=cellar_timeout) as cellar:
        auth_client = None
        if update_url:
            auth_client = httpx.Client(timeout=cellar_timeout, auth=httpx.DigestAuth(
                os.environ.get("VIRTUOSO_DBA_USER", "dba"),
                os.environ["VIRTUOSO_DBA_PASSWORD"]))
        for win_start, win_end in month_windows(start, end):
            tag = win_start[:7]
            manifest = write_artifact(
                fetch_window(cellar, win_start, win_end), out_dir, tag)
            logger.info("window %s: %d triples -> %s",
                        tag, manifest["triples"], manifest["artifact"])
            totals["windows"] += 1
            totals["triples"] += manifest["triples"]
            if auth_client and manifest["triples"]:
                totals["loaded"] += load_artifact(
                    auth_client, update_url,
                    out_dir / manifest["artifact"])
        if auth_client:
            auth_client.close()
    logger.info("mirror run done: %s", totals)
    print(json.dumps(totals))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
