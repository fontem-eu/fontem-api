"""Batch-translate :Authority names into the 24 EU languages via Mistral's
Batch API, then write ``name_<lang>`` back to Neo4j.

Why the Batch API: synchronous per-authority enrichment through the
consolidator runs ~11 s/authority (full rule pipeline + 23 sequential
translations); ~203k authorities would take weeks. Mistral's Batch API is
asynchronous and ~50 % cheaper: we hand it one JSONL of N requests, it
parallelises on its side, and we write the results straight to Neo4j —
bypassing the consolidator hot path entirely.

Flow (all in one run, fully automated):
  1. SELECT authorities that still miss at least one ``name_<lang>``.
  2. COMPILE a JSONL batch — one chat request per authority, each asked to
     return a JSON object mapping ISO-639-1 code -> translation.
  3. UPLOAD it to ``/v1/files`` (purpose=batch).
  4. CREATE a ``/v1/batch/jobs`` job (endpoint ``/v1/chat/completions``).
  5. POLL until the job reaches a terminal status.
  6. DOWNLOAD the output file and INTEGRATE: ``SET a.name_<lang>`` +
     ``a.name_lang`` (source) + ``a.multilingual_updated_at``.

Idempotent: re-running only re-selects authorities still missing a
translation, so a partial run is safe to repeat. ``--dry-run`` stops after
step 2 (writes the JSONL, makes no Mistral calls). ``--resume-job <id>``
skips compile/upload/create and jumps to poll+integrate for an existing job.

The authoritative multilingual store is Neo4j (the linguistics Postgres
``translations`` table is only a per-text cache); this writer targets Neo4j
exclusively, matching the ``name_<lang>`` convention the consolidator uses.

Env:
  MISTRAL_API_KEY            (required)   Mistral secret; must be a live key.
  MISTRAL_API_URL            https://api.mistral.ai/v1
  MISTRAL_BATCH_MODEL        mistral-medium-latest  (matches the service default)
  NEO4J_URI / NEO4J_PASSWORD (required)   / NEO4J_USER (default "neo4j")
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Final

import httpx
from neo4j import GraphDatabase

log = logging.getLogger("batch_translate_authorities")

# The 24 official EU languages (ISO-639-1), mirroring
# fontem-consolidator EU_OFFICIAL_LANGS. A name is never translated into its
# own source language, so a fully covered authority carries 23 name_<lang>.
EU_LANGS: Final[tuple[str, ...]] = (
    "bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "ga", "hr",
    "hu", "it", "lt", "lv", "mt", "nl", "pl", "pt", "ro", "sk", "sl", "sv",
)

# ISO-639-1 -> English language name, used to make the prompt unambiguous.
LANG_NAME: Final[dict[str, str]] = {
    "bg": "Bulgarian", "cs": "Czech", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian",
    "fi": "Finnish", "fr": "French", "ga": "Irish", "hr": "Croatian",
    "hu": "Hungarian", "it": "Italian", "lt": "Lithuanian", "lv": "Latvian",
    "mt": "Maltese", "nl": "Dutch", "pl": "Polish", "pt": "Portuguese",
    "ro": "Romanian", "sk": "Slovak", "sl": "Slovenian", "sv": "Swedish",
}

# Country (ISO-3166 alpha-3) -> primary official language (ISO-639-1),
# mirroring fontem-consolidator COUNTRY_PRIMARY_LANG. Used to infer the
# source language when the node carries no explicit name_lang.
COUNTRY_PRIMARY_LANG: Final[dict[str, str]] = {
    "AUT": "de", "BEL": "nl", "BGR": "bg", "HRV": "hr", "CYP": "el",
    "CZE": "cs", "DNK": "da", "EST": "et", "FIN": "fi", "FRA": "fr",
    "DEU": "de", "GRC": "el", "HUN": "hu", "IRL": "en", "ITA": "it",
    "LVA": "lv", "LTU": "lt", "LUX": "fr", "MLT": "mt", "NLD": "nl",
    "POL": "pl", "PRT": "pt", "ROU": "ro", "SVK": "sk", "SVN": "sl",
    "ESP": "es", "SWE": "sv",
}

# Cypher: authorities with a name but missing at least one EU-language
# translation. ORDER BY the staleness marker so re-runs make forward
# progress and never re-pick the same head set.
_SELECT = """
MATCH (a:Authority)
WHERE a.name IS NOT NULL AND a.authority_id IS NOT NULL
  AND any(code IN $langs WHERE a["name_" + code] IS NULL AND code <> coalesce(a.name_lang, ""))
RETURN a.authority_id AS id, a.name AS name, a.country AS country, a.name_lang AS name_lang
ORDER BY coalesce(a.multilingual_updated_at, datetime("1970-01-01")) ASC
LIMIT $limit
"""

_WRITE = """
UNWIND $rows AS row
MATCH (a:Authority {authority_id: row.id})
SET a += row.props,
    a.name_lang = coalesce(a.name_lang, row.src),
    a.multilingual_updated_at = datetime()
RETURN count(a) AS written
"""

_TERMINAL = {"SUCCESS", "FAILED", "TIMEOUT_EXCEEDED", "CANCELLED"}


def _source_lang(rec: dict) -> str:
    """Explicit name_lang wins; else the country's primary language; else en."""
    lang = (rec.get("name_lang") or "").strip().lower()
    if lang in EU_LANGS:
        return lang
    return COUNTRY_PRIMARY_LANG.get((rec.get("country") or "").upper(), "en")


def _targets(src: str) -> list:
    return [c for c in EU_LANGS if c != src]


def _build_request(rec: dict) -> dict:
    """One Mistral Batch line: {custom_id, body}. custom_id is the
    authority_id so the response maps straight back to the node. The model
    is set once at job creation, not per line."""
    src = _source_lang(rec)
    target_desc = ", ".join(f"{c} ({LANG_NAME.get(c, c)})" for c in _targets(src))
    system = (
        "You are a professional translator of official public-authority "
        "names for an EU procurement-transparency database."
    )
    user = (
        f"Translate this official public-authority name from "
        f"{LANG_NAME.get(src, src)} into the target languages below.\n\n"
        f"Name: \"{rec['name']}\"\n"
        f"Target languages (ISO-639-1): {target_desc}\n\n"
        "Rules: give the name as it officially appears in each language, or "
        "the most faithful translation where no official form exists; keep "
        "proper nouns, place names, acronyms and legal forms correct for that "
        "language; do not transliterate arbitrarily; no commentary. Return "
        "ONLY a JSON object mapping each target ISO-639-1 code to its "
        "translation string."
    )
    return {
        "custom_id": rec["id"],
        "body": {
            "max_tokens": 1024,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
    }


def compile_batch(records: list, path: str) -> int:
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(_build_request(rec), ensure_ascii=False) + "\n")
    log.info("compiled %d requests -> %s", len(records), path)
    return len(records)


class MistralBatch:
    """Thin client over the documented Batch API surface (verified live:
    /files, /batch/jobs, /batch/jobs/{id}, /files/{id}/content)."""

    def __init__(self, base: str, key: str):
        self._c = httpx.Client(
            base_url=base.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"},
            timeout=120.0,
        )

    def upload(self, path: str) -> str:
        with open(path, "rb") as fh:
            r = self._c.post(
                "/files",
                files={"file": (os.path.basename(path), fh, "application/jsonl")},
                data={"purpose": "batch"},
            )
        r.raise_for_status()
        fid = r.json()["id"]
        log.info("uploaded input file id=%s", fid)
        return fid

    def create_job(self, file_id: str, model: str) -> str:
        r = self._c.post("/batch/jobs", json={
            "input_files": [file_id],
            "model": model,
            "endpoint": "/v1/chat/completions",
            "metadata": {"job": "authority-name-translation"},
        })
        r.raise_for_status()
        jid = r.json()["id"]
        log.info("created batch job id=%s model=%s", jid, model)
        return jid

    def poll(self, job_id: str, interval: float) -> dict:
        while True:
            r = self._c.get(f"/batch/jobs/{job_id}")
            r.raise_for_status()
            job = r.json()
            log.info(
                "job %s status=%s succeeded=%s failed=%s total=%s",
                job_id, job.get("status"), job.get("succeeded_requests"),
                job.get("failed_requests"), job.get("total_requests"),
            )
            if job.get("status") in _TERMINAL:
                return job
            time.sleep(interval)

    def download(self, file_id: str) -> list:
        r = self._c.get(f"/files/{file_id}/content")
        r.raise_for_status()
        return [json.loads(line) for line in r.text.splitlines() if line.strip()]

    def close(self) -> None:
        self._c.close()


def _extract_translations(line: dict):
    """Pull the JSON translation map out of one Batch output line. Defensive:
    tolerates ```json fences and non-EU / empty keys. Returns a dict or None."""
    resp = line.get("response") or {}
    body = resp.get("body") if isinstance(resp, dict) else None
    if not body:
        return None
    try:
        content = body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    if content.startswith("```"):
        parts = content.split("```")
        content = parts[1] if len(parts) > 1 else content
        if content.lower().startswith("json"):
            content = content[4:]
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    out = {}
    for code, val in raw.items():
        code = str(code).strip().lower()
        if code in EU_LANGS and isinstance(val, str) and val.strip():
            out[code] = val.strip()
    return out or None


def _rows_from_lines(lines: list, by_id: dict) -> tuple:
    """Turn Batch output lines into Neo4j write rows. Returns (rows, skipped)."""
    rows, skipped = [], 0
    for line in lines:
        rec = by_id.get(line.get("custom_id"))
        translations = _extract_translations(line) if rec else None
        if not rec or not translations:
            skipped += 1
            continue
        src = _source_lang(rec)
        props = {f"name_{code}": text
                 for code, text in translations.items() if code != src}
        if not props:
            skipped += 1
            continue
        rows.append({"id": rec["id"], "props": props, "src": src})
    return rows, skipped


# Mistral Batch pricing (USD per 1M tokens) = 50% of standard list. Defaults
# are mistral-medium (std $1.5 in / $7.5 out -> batch $0.75 / $3.75);
# override via env for mistral-small (~10x cheaper) or when list prices move.
_PRICE_IN_PER_M = float(os.environ.get("MISTRAL_BATCH_PRICE_IN_PER_M", "0.75"))
_PRICE_OUT_PER_M = float(os.environ.get("MISTRAL_BATCH_PRICE_OUT_PER_M", "3.75"))


def _cost_report(lines: list) -> dict:
    """Sum token usage across Batch output lines and estimate spend. Token
    counts are exact (from each response's ``usage``); the USD figure is an
    estimate at the configured batch rate. Logged so a run's cost is visible
    and can be extrapolated to the full 203k-authority backfill."""
    tin = tout = counted = 0
    for line in lines:
        body = (line.get("response") or {}).get("body") or {}
        usage = body.get("usage") or {}
        if usage:
            tin += usage.get("prompt_tokens", 0)
            tout += usage.get("completion_tokens", 0)
            counted += 1
    usd = tin / 1e6 * _PRICE_IN_PER_M + tout / 1e6 * _PRICE_OUT_PER_M
    report = {"responses_with_usage": counted, "prompt_tokens": tin,
              "completion_tokens": tout, "total_tokens": tin + tout,
              "est_usd": round(usd, 4),
              "rate_in_per_m": _PRICE_IN_PER_M, "rate_out_per_m": _PRICE_OUT_PER_M}
    per = round(usd / counted, 5) if counted else 0.0
    log.info("COST: %s (~$%.5f/authority -> ~$%.0f for 203k)", report, per, per * 203000)
    return report


def integrate(driver, lines: list, by_id: dict, batch: int = 500) -> dict:
    """Write name_<lang> back to Neo4j. Returns counts."""
    rows, skipped = _rows_from_lines(lines, by_id)
    written = 0
    with driver.session() as session:
        for i in range(0, len(rows), batch):
            written += session.run(_WRITE, rows=rows[i:i + batch]).single()["written"]
    summary = {"parsed": len(rows), "skipped": skipped, "written": written,
               "langs_total": sum(len(r["props"]) for r in rows)}
    log.info("integrate: %s", summary)
    return summary


def _driver():
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )


def _select(driver, limit: int) -> list:
    with driver.session() as session:
        recs = [dict(r) for r in session.run(_SELECT, langs=list(EU_LANGS), limit=limit)]
    log.info("selected %d authorities needing translation", len(recs))
    return recs


def _submit_and_integrate(driver, records: list, args) -> dict:
    """Upload/create (unless resuming), poll, download, write back."""
    by_id = {r["id"]: r for r in records}
    client = MistralBatch(
        os.environ.get("MISTRAL_API_URL", "https://api.mistral.ai/v1"),
        os.environ["MISTRAL_API_KEY"],
    )
    try:
        if args.resume_job:
            job_id = args.resume_job
        else:
            with open(args.jsonl_path, encoding="utf-8") as fh:
                sample = fh.readline().strip()
            log.info("sample request: %s", sample[:400])
            file_id = client.upload(args.jsonl_path)
            job_id = client.create_job(file_id, args.model)
        job = client.poll(job_id, args.poll_interval)
        if job.get("status") != "SUCCESS":
            log.error("job %s ended %s (failed=%s); integrating any output",
                      job_id, job.get("status"), job.get("failed_requests"))
        out_id = job.get("output_file")
        if not out_id:
            return {"job_id": job_id, "status": job.get("status"),
                    "written": 0, "error": "no output_file"}
        lines = client.download(out_id)
        summary = integrate(driver, lines, by_id)
        summary["cost"] = _cost_report(lines)
        summary.update({"job_id": job_id, "status": job.get("status")})
        return summary
    finally:
        client.close()


def run(args) -> dict:
    driver = _driver()
    try:
        records = _select(driver, args.limit)
        if not records:
            return {"selected": 0}
        if not args.resume_job:
            compile_batch(records, args.jsonl_path)
            if args.dry_run:
                log.info("dry-run: stopping after compile (%s)", args.jsonl_path)
                return {"selected": len(records), "compiled": len(records),
                        "dry_run": True, "jsonl_path": args.jsonl_path}
        return _submit_and_integrate(driver, records, args)
    finally:
        driver.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Batch-translate authority names via Mistral.")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--model", default=os.environ.get(
        "MISTRAL_BATCH_MODEL", "mistral-medium-latest"))
    ap.add_argument("--dry-run", action="store_true",
                    help="compile the JSONL and stop; no Mistral calls")
    ap.add_argument("--poll-interval", type=float, default=30.0)
    ap.add_argument("--resume-job", default=None,
                    help="skip compile/submit; poll+integrate this job id")
    ap.add_argument("--jsonl-path", default="/tmp/authority_translations.jsonl")
    log.info("done %s", run(ap.parse_args()))


if __name__ == "__main__":
    main()
