"""Language (ISO-639-1) plumbing for Neo4j-backed handlers.

The :Authority nodes carry `name_bg, name_cs, …, name_sv` written by
`gmr-consolidator`'s TranslationEnrichmentAuthority rule. To surface a
translated name we build Cypher like::

    coalesce(a.name_pl, a.name) AS name

`locale` is NEVER a bind parameter because Cypher property names can't
be parameterised — the property name is part of the query shape. That
makes strict whitelisting mandatory. `safe_lang()` rejects anything
outside the 24 EU codes (including case/region variants like ``en-GB``,
empty strings, garbage, injection attempts); `authority_name_expr()`
only ever inlines a whitelisted two-letter code, and falls back to a
bare ``a.name`` for `None`.
"""
from __future__ import annotations

from typing import Final

# The 24 EU official ISO-639-1 codes, in Official Journal order. Must
# stay in sync with:
#   - gmr-linguistics  src/domain/languages.py           EU_OFFICIAL_LANGS
#   - gmr-consolidator src/consolidator/clients/linguistics.py  EU_OFFICIAL_LANGS
#   - gmr-web          src/composables/eu-languages.js         EU_CODES
EU_LANGS: Final[frozenset[str]] = frozenset({
    "bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr",
    "ga", "hr", "hu", "it", "lt", "lv", "mt", "nl", "pl", "pt",
    "ro", "sk", "sl", "sv",
})


def safe_lang(value: str | None) -> str | None:
    """Return a whitelisted EU language code or None.

    Accepts ``"EN"``, ``"fr-FR"``, ``"pt_BR"``, etc. — strips to the first
    two lowercase letters before checking. Anything else (including
    ``""``, ``None``, non-EU codes, symbols) maps to ``None``.
    """
    if not value or not isinstance(value, str):
        return None
    code = value.strip()[:2].lower()
    return code if code in EU_LANGS else None


def authority_name_expr(alias: str, lang: str | None) -> str:
    """Cypher fragment selecting an Authority's name in the requested
    language with a fallback to the stored original.

    Returns::

        coalesce(<alias>.name_<lang>, <alias>.name)   when lang is a
                                                      whitelisted code
        <alias>.name                                   otherwise

    The alias is trusted (caller-controlled, e.g. ``"a"``) and the lang
    is trust-by-construction (only ever a whitelisted string, never
    user input).
    """
    if not lang:
        return f"{alias}.name"
    # lang is from the whitelist; safe to inline.
    return f"coalesce({alias}.name_{lang}, {alias}.name)"


def contract_title_expr(alias: str, lang: str | None) -> str:
    """Cypher fragment selecting a title in the requested language with a
    fallback to the stored original.

    Mirrors ``authority_name_expr`` — same safety contract: ``lang`` must
    have passed ``safe_lang`` (whitelisted two-letter code or None).
    :Contract and :Disclosure (cohesion grant) titles carry their
    translations as ``title_<lang>``, written by the neo4j sink from
    fontem-translator's TranslateContractTitle / TranslateDisclosureTitle
    events. The source language itself is never among them, so asking for
    it falls through to the original.
    """
    if not lang:
        return f"{alias}.title"
    return f"coalesce({alias}.title_{lang}, {alias}.title)"


def title_original_expr(alias: str, lang: str | None) -> str:
    """Cypher fragment for the stored original title, but only when
    ``contract_title_expr`` would show a translation; null otherwise.

    A machine translation is shown in place of what the buyer published,
    so the reader must be able to see the original: the UI offers it
    wherever this is not null. Same safety contract as above.
    """
    if not lang:
        return "null"
    return f"CASE WHEN {alias}.title_{lang} IS NOT NULL THEN {alias}.title END"
