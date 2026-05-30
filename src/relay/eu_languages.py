"""BCP-47 language codes whose Wikidata labels, descriptions, and
aliases we accept into Virtuoso.

When a Wikidata edit is *only* a label/description/alias change in a
language outside this set, the relay drops the event before it ever
becomes a `dirty_entities` row — so the worker doesn't spend an API
roundtrip refetching an entity whose only delta is a translation we
won't surface anywhere.

Membership:
  * EU-official languages (all 24 from Regulation 1/1958, as amended)
  * ``mul`` — Wikidata's "multilingual" / language-neutral bucket. Used
    for monolingual values that have no real language (a Latin binomial,
    a chemical formula, an ISO currency code). Always kept.
  * ``en`` — kept even though English is no longer EU-official
    post-Brexit. Every press kit, every report, and every internal
    interaction with the platform is in English; removing it would gut
    usability for the team and beta users.

Adding a language is cheap to do, but expensive in operations: each one
expands the worker's Wikidata API load and the Virtuoso write volume in
roughly proportion to that language's edit-rate on Wikidata. So:
default closed for non-EU, only open up when there's a use-case.
"""
from __future__ import annotations

EU_LANGUAGES: frozenset[str] = frozenset({
    "bg",   # Bulgarian
    "cs",   # Czech
    "da",   # Danish
    "de",   # German
    "el",   # Greek
    "en",   # English (see docstring)
    "es",   # Spanish
    "et",   # Estonian
    "fi",   # Finnish
    "fr",   # French
    "ga",   # Irish
    "hr",   # Croatian
    "hu",   # Hungarian
    "it",   # Italian
    "lt",   # Lithuanian
    "lv",   # Latvian
    "mt",   # Maltese
    "mul",  # Multilingual / language-neutral (see docstring)
    "nl",   # Dutch
    "pl",   # Polish
    "pt",   # Portuguese
    "ro",   # Romanian
    "sk",   # Slovak
    "sl",   # Slovenian
    "sv",   # Swedish
})
