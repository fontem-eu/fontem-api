"""Pre-filter for raw Wikimedia recentchange events.

The relay consumes ~10 events/sec average from `wikidatawiki`. Naively
upserting every one into ``wikidata.dirty_entities`` would burn Postgres
write IO on edits whose entire delta is "added a Bengali description"
or "linked a sitelink to ja.wikipedia" — neither of which we surface.

This module decides, per event, one of three things:

  * ``EventAction.DIRTY``    — entity is touched, mark it for refetch.
  * ``EventAction.DELETED``  — entity was deleted on Wikidata, the worker
                               should remove it from Virtuoso. No fetch.
  * ``EventAction.IGNORE``   — event has no bearing on what we store.

Design rule: *ignore-list, not allow-list*. The default verdict for any
unrecognised shape is ``DIRTY`` (fail-open). We only ignore patterns we
have positively identified as not-of-interest, because the cost of a
wrongly-dropped event is staleness in Virtuoso (silent), whereas the
cost of a wrongly-kept event is one API roundtrip we didn't need
(observable, bounded).

Two narrow ignore rules live here:

  1. Sitelink-only edits (``wbsetsitelink-*``, ``clientsitelink-*``):
     sitelinks are inter-wiki page references that we do not ingest at
     all, so any edit whose sole action is a sitelink change is dead
     weight.
  2. Label / description / alias edits *in a non-EU language*: an
     edit comment like ``/* wbsetdescription-add:1|bn */`` advertises
     that the edit added a Bengali description; if Bengali is not in
     ``EU_LANGUAGES`` we don't carry the new value anywhere.

Anything else — including statement creates/updates/removes, redirects,
merges, undos, bulk edits, or events whose comment doesn't parse —
returns DIRTY. We deliberately err on the side of doing the API call.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from src.relay.eu_languages import EU_LANGUAGES


class EventAction(enum.Enum):
    """Verdict for an incoming SSE event."""

    DIRTY = "dirty"      # mark entity for refetch
    DELETED = "deleted"  # entity removed on Wikidata, tombstone in Virtuoso
    IGNORE = "ignore"    # not relevant to our state


@dataclass(frozen=True)
class FilterDecision:
    """The relay records ``comment_kind`` even on IGNORE so that the
    dirty_entities table (or, for IGNORE, the relay log) gives a
    forensic trail of which auto-comment classes are filtering out
    real volume in production."""

    action: EventAction
    # e.g. "wbsetdescription-add", "log-delete-delete", None if unparseable
    comment_kind: str | None


# Wikimedia auto-comments wrap the action prefix in /* ... */, e.g.
#   /* wbsetdescription-add:1|bn */ free-form translator text...
# The prefix runs until the first colon or space. Capturing only the
# kind (without the count or lang suffix) — that comes from a second
# regex below if relevant.
_COMMENT_KIND_RE = re.compile(r"/\*\s*([a-zA-Z][a-zA-Z\-]*)")

# Pull the BCP-47 language tag out of language-scoped actions:
#   /* wbsetlabel-add:1|fr */ ...
#   /* wbsetdescription-set:1|en-gb */ ...
#   /* wbeditentity-update-languages-short:0||de */ QuickStatements 3.0
# The wbeditentity-update-languages-short format uses a double-pipe
# (the empty middle field is the entity-id slot, which is implicit in
# the URL). The regex accepts both single and double `|` separator
# styles. Wikidata uses the bare language subtag (en-gb, zh-hans etc.).
_LANG_SUFFIX_RE = re.compile(
    r"(?:wbset(?:label|description|aliases)-(?:add|set|remove)"
    r"|wbeditentity-update-languages-short)"
    r":[0-9]+\|\|?"
    r"([a-zA-Z]+(?:-[a-zA-Z]+)*)"
)

# Comment-kind prefixes that *only* touch sitelinks. Anything matching
# these is dropped unconditionally — there is no ``...|<lang>`` to
# decide on, and we don't ingest sitelinks.
_SITELINK_PREFIXES: frozenset[str] = frozenset({
    "wbsetsitelink-add",
    "wbsetsitelink-set",
    "wbsetsitelink-remove",
    "wbsetsitelink-update",
    "clientsitelink-update",
    "clientsitelink-remove",
})

# Comment-kind prefixes that are language-scoped. If the parsed lang is
# not in EU_LANGUAGES, the event is dropped. If we can't parse a lang
# from the comment for one of these kinds (rare malformed comments),
# we fail open and keep the event.
_LANG_SCOPED_PREFIXES: frozenset[str] = frozenset({
    "wbsetlabel-add",
    "wbsetlabel-set",
    "wbsetlabel-remove",
    "wbsetdescription-add",
    "wbsetdescription-set",
    "wbsetdescription-remove",
    "wbsetaliases-add",
    "wbsetaliases-set",
    "wbsetaliases-remove",
    # QuickStatements-driven bulk lang edit (one specific language per
    # comment, format `:0||<lang>`). Was ~21% of post-relay dirty
    # entries before this was added; nearly all of them are non-EU
    # langs (bn, hi, ml) so we drop them at the source.
    "wbeditentity-update-languages-short",
})


def _parse_comment_kind(comment: str | None) -> str | None:
    if not comment:
        return None
    match = _COMMENT_KIND_RE.match(comment)
    return match.group(1) if match else None


def _parse_lang_suffix(comment: str) -> str | None:
    match = _LANG_SUFFIX_RE.search(comment)
    return match.group(1).lower() if match else None


def classify(raw: dict) -> FilterDecision:  # pylint: disable=too-many-return-statements
    """Decide what the relay should do with a raw SSE event dict.

    Caller is responsible for the wiki-level filter (``wiki ==
    'wikidatawiki'``) — that's an earlier gate that doesn't belong
    here. This function assumes the event is already on the right wiki.
    """
    edit_type = raw.get("type")
    comment = raw.get("comment") or ""

    # Deletion: only `log_type=delete AND log_action=delete` is a real
    # entity deletion. `delete/restore` is an undelete (treat as a
    # normal dirty bump — the entity is back, refetch it).
    # `delete/revision` is a hidden-revision action, the entity itself
    # is unaffected.
    if edit_type == "log":
        log_type = raw.get("log_type")
        log_action = raw.get("log_action")
        if log_type == "delete" and log_action == "delete":
            return FilterDecision(EventAction.DELETED, "log-delete-delete")
        if log_type == "delete" and log_action == "restore":
            return FilterDecision(EventAction.DIRTY, "log-delete-restore")
        # All other log actions — patrol, abusefilter, newusers, move,
        # block, thanks, etc. — are noise from our perspective.
        return FilterDecision(EventAction.IGNORE, f"log-{log_type}-{log_action}")

    # `categorize` is wiki-page categorisation; never applies to
    # Wikidata entities, but the namespace check on `wiki` doesn't
    # exclude it. Drop it explicitly so the comment_kind reads
    # cleanly.
    if edit_type == "categorize":
        return FilterDecision(EventAction.IGNORE, "categorize")

    kind = _parse_comment_kind(comment)

    # Sitelink-only edits: no ambiguity, no language tag to consider.
    if kind in _SITELINK_PREFIXES:
        return FilterDecision(EventAction.IGNORE, kind)

    # Language-scoped label/description/alias edits.
    if kind in _LANG_SCOPED_PREFIXES:
        lang = _parse_lang_suffix(comment)
        if lang is None:
            # Malformed comment for a known kind — fail open.
            return FilterDecision(EventAction.DIRTY, kind)
        if lang in EU_LANGUAGES:
            return FilterDecision(EventAction.DIRTY, kind)
        return FilterDecision(EventAction.IGNORE, kind)

    # Everything else — statement creates/updates/removes, redirects,
    # merges, undos, bulk `wbeditentity-*`, empty comments, unknown
    # kinds — defaults to DIRTY.
    return FilterDecision(EventAction.DIRTY, kind)
