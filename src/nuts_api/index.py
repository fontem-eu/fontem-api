"""Read model over the bundled gazetteer: records, hierarchy, name index.

Everything here is derived once from the gazetteer and cached, because the
gazetteer is a static file bundled in the image: nothing it describes can
change between requests, and folding ~40k names per request would be the
dominant cost of every call.

The one out-of-module import is ``src.data.nuts_gazetteer``, which owns
reading and folding the artifact. Nothing in ``src.api`` is imported — this
module is meant to lift out into its own service, like ``src.atlas_api``.
"""
from __future__ import annotations

import functools

from src.data import nuts_gazetteer

# Where a matched form came from, and how strongly it counts. A name in the
# language you asked for beats the same name in another language, which beats
# an alias — someone typing "Lisbonne" with lang=fr wants the French label
# first, not a Portuguese alias that happens to contain it.
RANK_EXACT_CODE = 0
RANK_NAME_REQUESTED_LANG = 1
RANK_EUROSTAT = 2
RANK_NAME_OTHER_LANG = 3
RANK_ALIAS = 4
RANK_CONTAINS = 5


def _country_of(code: str) -> str:
    return code[:2].upper()


def _parent_of(code: str) -> str | None:
    return code[:-1] if len(code) > 2 else None


@functools.lru_cache(maxsize=1)
def records() -> dict[str, dict]:
    """``code -> the full public record``, in code order."""
    out: dict[str, dict] = {}
    for code, entry in sorted((nuts_gazetteer.document().get("regions") or {}).items()):
        out[code] = {
            "code": code,
            "level": entry.get("level", max(0, len(code) - 2)),
            "country": _country_of(code),
            "parent": _parent_of(code),
            "name_native": entry.get("native") or "",
            "name_latn": entry.get("latn") or "",
            "names": dict(entry.get("labels") or {}),
            "aliases": list(entry.get("aliases") or ()),
            "sources": list(entry.get("src") or ()),
        }
    return out


@functools.lru_cache(maxsize=1)
def children_of() -> dict[str, list[str]]:
    """``code -> direct children``. Derived from the codes, which nest by prefix."""
    out: dict[str, list[str]] = {}
    for code in records():
        parent = _parent_of(code)
        if parent:
            out.setdefault(parent, []).append(code)
    return out


def ancestors_of(code: str) -> list[str]:
    """Parent chain, closest first. A NUTS code is its own path."""
    known = records()
    chain = []
    current = _parent_of(code)
    while current:
        if current in known:
            chain.append(current)
        current = _parent_of(current)
    return chain


@functools.lru_cache(maxsize=1)
def name_index() -> tuple[tuple[str, str, str | None, str], ...]:
    """Every searchable form: ``(folded, code, language, kind)``.

    Flat and pre-folded so a search is one scan over ~40k tuples rather than
    a fold of every name on every request. Kept as a tuple so the cache
    cannot be mutated by a caller.
    """
    rows: list[tuple[str, str, str | None, str]] = []
    for code, rec in records().items():
        rows.append((code.lower(), code, None, "code"))
        if rec["name_native"]:
            rows.append((nuts_gazetteer.fold(rec["name_native"]), code, None, "native"))
        if rec["name_latn"]:
            rows.append((nuts_gazetteer.fold(rec["name_latn"]), code, None, "latn"))
        for lang, name in rec["names"].items():
            rows.append((nuts_gazetteer.fold(name), code, lang, "name"))
        for alias in rec["aliases"]:
            rows.append((nuts_gazetteer.fold(alias), code, None, "alias"))
    return tuple(rows)


def _display(rec: dict, lang: str) -> str:
    return rec["names"].get(lang) or rec["name_latn"] or rec["name_native"] or rec["code"]


def _unfolded(rec: dict, folded: str, lang: str | None, kind: str) -> str:
    """The original spelling behind a folded match, for showing back."""
    if kind == "code":
        return rec["code"]
    if kind == "native":
        return rec["name_native"]
    if kind == "latn":
        return rec["name_latn"]
    if kind == "name" and lang:
        return rec["names"].get(lang, folded)
    for alias in rec["aliases"]:
        if nuts_gazetteer.fold(alias) == folded:
            return alias
    return folded


def _rank(folded_query: str, folded_form: str, lang: str | None,
          kind: str, requested: str) -> int | None:
    """How strong a hit this form is, or None when it does not match."""
    if kind == "code":
        if folded_form == folded_query:
            return RANK_EXACT_CODE
        return RANK_CONTAINS if folded_form.startswith(folded_query) else None
    prefix = folded_form.startswith(folded_query)
    word = f" {folded_query}" in folded_form
    if not prefix and not word:
        return RANK_CONTAINS if folded_query in folded_form else None
    if kind == "name":
        return RANK_NAME_REQUESTED_LANG if lang == requested else RANK_NAME_OTHER_LANG
    if kind in ("native", "latn"):
        return RANK_EUROSTAT
    return RANK_ALIAS


def _best_per_region(folded: str, lang: str, level: int | None,
                     country: str | None) -> dict[str, tuple[int, str, str | None, str]]:
    """Strongest match per region: ``code -> (rank, folded form, language, kind)``.

    One region can match through several of its names — Lisbon's has a dozen —
    and a caller should never have to deduplicate that.
    """
    known = records()
    best: dict[str, tuple[int, str, str | None, str]] = {}
    for folded_form, code, form_lang, kind in name_index():
        rank = _rank(folded, folded_form, form_lang, kind, lang)
        if rank is None:
            continue
        record = known[code]
        if level is not None and record["level"] != level:
            continue
        if country and record["country"] != country.upper():
            continue
        current = best.get(code)
        if current is None or rank < current[0]:
            best[code] = (rank, folded_form, form_lang, kind)
    return best


def search(query: str, lang: str, *, level: int | None = None,
           country: str | None = None, limit: int = 20) -> tuple[int, list[dict]]:
    """Name (in any of the 24 languages) or code → NUTS regions, ranked.

    Returns the total number of matching regions and the first `limit` of them.
    """
    folded = nuts_gazetteer.fold(query)
    if not folded:
        return 0, []
    known = records()
    ordered = sorted(_best_per_region(folded, lang, level, country).items(),
                     key=lambda kv: (kv[1][0], kv[0]))
    matches = []
    for code, (rank, folded_form, form_lang, kind) in ordered[:limit]:
        record = known[code]
        matches.append({
            "code": code,
            "level": record["level"],
            "country": record["country"],
            "name": _display(record, lang),
            "name_native": record["name_native"],
            "matched": _unfolded(record, folded_form, form_lang, kind),
            "matched_language": form_lang,
            "matched_kind": kind,
            "rank": rank,
        })
    return len(ordered), matches
