"""What a petition and a lobbying registrant say, in the reader's language.

One rule each, read by every surface that shows them: the petition and
lobbyist pages, and search cards (through the contract data source).
"""
from __future__ import annotations

import re
from typing import Any

from src.api.lang import EU_LANGS


#: Per-language properties: read through petition_texts, not returned raw.
_PER_LANGUAGE = re.compile(r"^(?:title|objectives|annex_text|objectives_summary)_[a-z]{2}$")


def petition_texts(node: dict[str, Any], lang: str | None) -> dict[str, Any]:
    """The petition's texts in the reader's language, beside the original.

    Title, objectives and annex come from the register's official
    versions (title_<lang>, ...); the summary is machine-written, from the
    English objectives, and shown only while it summarises them as they
    read now. Without a version in ``lang``, the English one stands."""
    out = {k: v for k, v in node.items() if not _PER_LANGUAGE.match(k)}
    shown = lang if lang and node.get(f"title_{lang}") else None
    original = node.get("title_lang")
    for field in ("title", "objectives", "annex_text"):
        if shown and node.get(f"{field}_{shown}"):
            out[field] = node[f"{field}_{shown}"]
        out[f"{field}_original"] = (node.get(f"{field}_{original}") if original else None) \
            or node.get(field)
    out["summary"] = None
    if node.get("objectives_summarized_from") == node.get("objectives"):
        in_lang = node.get(f"objectives_summary_{lang}") if lang else None
        out["summary"] = in_lang or node.get("objectives_summary_en")
    out["language_shown"] = shown or "en"
    out["languages"] = sorted(code for code in EU_LANGS if node.get(f"title_{code}"))
    return out


def goals_in_language(node: dict[str, Any], lang: str | None) -> dict[str, Any]:
    """The goals and their summary in the reader's language, where a
    translation of the goals as they read now exists; else as written.
    A translation or summary made from goals since rewritten is not shown."""
    goals = node.get("detail_goals")
    source = node.get("detail_goals_lang")
    translated = None
    if lang and lang != source and node.get("detail_goals_translated_from") == goals:
        translated = node.get(f"detail_goals_{lang}")
    summary, summary_lang = None, None
    if node.get("detail_goals_summarized_from") == goals:
        for code in (lang, source):
            if code and node.get(f"detail_goals_summary_{code}"):
                summary, summary_lang = node[f"detail_goals_summary_{code}"], code
                break
    return {
        "goals": translated or goals,
        "goals_original": goals,
        "goals_lang": source,
        "goals_translated": translated is not None,
        "goals_summary": summary,
        # The reader's language, or the goals' own where none was made in it.
        "goals_summary_lang": summary_lang,
    }
