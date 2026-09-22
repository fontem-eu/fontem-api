"""Names for NUTS regions, in the 24 official EU languages.

Reads the gazetteer built by ``src.etl.build_nuts_gazetteer`` (bundled at
``src/data/nuts/nuts_names.json``) and turns it into what its two readers
need: the region picker: one display name per language, the two Eurostat forms, and a folded
search string that matches the region under any of its names.

Eurostat names a region twice — in the national language and transliterated
into Latin — so a picker fed from it alone cannot find ``Αττική`` from
"Attica", and 152 regions across EL, BG, RS, MK, CY and ME are effectively
invisible to anyone not typing Greek or Cyrillic. The gazetteer adds the
translations and aliases; this module decides which one to show.

Everything is computed once per language and cached: the file is static,
bundled in the image, and 1.8k regions is small enough to keep resident.
"""
from __future__ import annotations

import functools
import json
import os
import unicodedata

from src.data import geo_ip

_GAZETTEER_PATH = os.path.join(os.path.dirname(__file__), "nuts", "nuts_names.json")

DEFAULT_LANGUAGE = "en"

# NUTS-0 codes that differ from the country's ISO alpha-2, which is what
# geo_ip keys its language map by.
_NUTS0_ALIAS = {"EL": "GR", "UK": "GB"}


def fold(text: str) -> str:
    """Case- and diacritic-insensitive form used for search matching.

    ``Ática`` → ``atica``, ``Αττική`` → ``αττικη``. Must agree with
    ``foldText()`` in the web app and ``fold()`` in the builder: a query the
    server would match has to match client-side too, or the same keystrokes
    rank differently depending on who does the filtering.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


@functools.lru_cache(maxsize=1)
def document() -> dict:
    """The gazetteer as built, or an empty one if it isn't bundled."""
    if not os.path.isfile(_GAZETTEER_PATH):
        return {"languages": [DEFAULT_LANGUAGE], "regions": {}}
    with open(_GAZETTEER_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def languages() -> tuple[str, ...]:
    return tuple(document().get("languages") or (DEFAULT_LANGUAGE,))


def resolve_language(raw: str | None) -> str:
    """Normalise a caller's language tag to one the gazetteer carries.

    ``pt-BR`` → ``pt``, ``EL`` → ``el``, anything unknown → English. The
    web app sends its active locale, which is a bare EU-24 code, but the
    endpoint is public and agents send whatever they have.
    """
    if not raw:
        return DEFAULT_LANGUAGE
    primary = raw.strip().replace("_", "-").split("-")[0].lower()
    return primary if primary in languages() else DEFAULT_LANGUAGE


def _native_first(code: str, language: str) -> bool:
    """Whether the national-language name is the right one for this reader.

    A Greek reader looking at a Greek region wants ``Κεντρικός Τομέας
    Αθηνών``, not its transliteration — so when the requested language is
    the region's own, the native name outranks the Latin one.
    """
    nuts0 = code[:2].upper()
    spoken = geo_ip.language_for_country(_NUTS0_ALIAS.get(nuts0, nuts0))
    return spoken == language


def _display_name(code: str, entry: dict, language: str) -> tuple[str, str]:
    """Best name for one region in one language, with where it came from."""
    label = (entry.get("labels") or {}).get(language)
    if label:
        return label, language
    native, latn = entry.get("native") or "", entry.get("latn") or ""
    if native and _native_first(code, language):
        return native, "native"
    if latn:
        return latn, "latn"
    if native:
        return native, "native"
    return code, "code"


def _search_terms(entry: dict, extra: str) -> str:
    """Every name this region answers to, folded and deduplicated.

    Substrings of another term are dropped — matching is prefix/contains, so
    ``atika`` inside ``atika periferija`` is already covered — which keeps
    the payload down without narrowing what matches.
    """
    forms = {entry.get("native") or "", entry.get("latn") or "", extra}
    forms |= set((entry.get("labels") or {}).values())
    forms |= set(entry.get("aliases") or ())
    folded = sorted({fold(f) for f in forms if f}, key=len)
    kept = [f for i, f in enumerate(folded)
            if not any(f in longer for longer in folded[i + 1:])]
    return " ".join(kept)


@functools.lru_cache(maxsize=1)
def search_index() -> dict[str, str]:
    """``code -> every name it answers to``, folded, across all languages.

    Language-independent on purpose: somebody reading the site in Portuguese
    still finds Attica by typing "Attica", and an index that had to be
    refetched on every language switch would be refetched for nothing.
    """
    return {code: _search_terms(entry, "")
            for code, entry in (document().get("regions") or {}).items()}


@functools.lru_cache(maxsize=32)
def localised(language: str) -> dict[str, dict]:
    """``code -> {name, name_latn, name_native, name_source, level}``.

    Cached per language: the gazetteer never changes at runtime, and folding
    ~35k names on every request would dominate the response time.
    """
    out: dict[str, dict] = {}
    for code, entry in (document().get("regions") or {}).items():
        name, source = _display_name(code, entry, language)
        out[code] = {
            "level": entry.get("level", len(code) - 2 if len(code) > 2 else 0),
            "name": name,
            "name_source": source,
            "name_latn": entry.get("latn") or "",
            "name_native": entry.get("native") or "",
        }
    return out
