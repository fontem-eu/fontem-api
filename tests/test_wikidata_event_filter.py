"""Unit tests for the relay-side pre-filter.

Filter mistakes are operationally invisible: a wrongly-ignored event
shows up as a stale entity in Virtuoso, with no error in any log.
These tests pin the rules so a refactor can't silently lose a case.
"""
from __future__ import annotations

from src.relay.event_filter import EventAction, classify


def _edit(comment: str = "", **extra) -> dict:
    base = {"type": "edit", "wiki": "wikidatawiki",
            "title": "Q42", "comment": comment}
    base.update(extra)
    return base


def _log(log_type: str, log_action: str, **extra) -> dict:
    base = {"type": "log", "wiki": "wikidatawiki", "title": "Q42",
            "log_type": log_type, "log_action": log_action}
    base.update(extra)
    return base


# ---------------------------------------------------------------- deletes


def test_delete_delete_marks_deleted() -> None:
    ev = _log("delete", "delete")
    out = classify(ev)
    assert out.action is EventAction.DELETED
    assert out.comment_kind == "log-delete-delete"


def test_delete_restore_is_dirty_not_ignored() -> None:
    # Restoring a previously-deleted entity is a real event we need
    # to act on — refetch the entity to populate triples again.
    ev = _log("delete", "restore")
    out = classify(ev)
    assert out.action is EventAction.DIRTY


def test_delete_revision_is_ignored() -> None:
    # revision-level deletion hides specific past revisions; the
    # current entity state is unchanged so we have nothing to do.
    ev = _log("delete", "revision")
    out = classify(ev)
    assert out.action is EventAction.IGNORE


def test_other_log_actions_are_ignored() -> None:
    for log_type, log_action in [
        ("patrol", "patrol"),
        ("abusefilter", "hit"),
        ("newusers", "create"),
        ("thanks", "thank"),
        ("block", "block"),
        ("move", "move"),
        ("rights", "rights"),
        ("protect", "protect"),
    ]:
        out = classify(_log(log_type, log_action))
        assert out.action is EventAction.IGNORE, (log_type, log_action)


# ---------------------------------------------------------------- sitelinks


def test_sitelink_only_edit_is_ignored() -> None:
    ev = _edit("/* wbsetsitelink-add:1|enwiki */ Apple Inc.")
    assert classify(ev).action is EventAction.IGNORE


def test_clientsitelink_update_is_ignored() -> None:
    ev = _edit("/* clientsitelink-update */ ")
    assert classify(ev).action is EventAction.IGNORE


# ---------------------------------------------------------- lang-scoped


def test_label_add_in_eu_language_is_dirty() -> None:
    assert classify(_edit("/* wbsetlabel-add:1|fr */ Pomme")).action \
        is EventAction.DIRTY


def test_description_add_in_non_eu_language_is_ignored() -> None:
    # Bengali — not in EU_LANGUAGES.
    out = classify(_edit("/* wbsetdescription-add:1|bn */ ফল"))
    assert out.action is EventAction.IGNORE
    assert out.comment_kind == "wbsetdescription-add"


def test_alias_add_in_non_eu_language_is_ignored() -> None:
    assert classify(_edit("/* wbsetaliases-add:1|hi */ सेब")).action \
        is EventAction.IGNORE


def test_quickstatements_languages_short_non_eu_is_ignored() -> None:
    # Format: `wbeditentity-update-languages-short:0||<lang>` — note
    # the double-pipe, which is why the regex had to grow. Bengali
    # is non-EU so this should be dropped.
    out = classify(_edit(
        "/* wbeditentity-update-languages-short:0||bn */ QuickStatements 3.0"
    ))
    assert out.action is EventAction.IGNORE
    assert out.comment_kind == "wbeditentity-update-languages-short"


def test_quickstatements_languages_short_eu_is_dirty() -> None:
    out = classify(_edit(
        "/* wbeditentity-update-languages-short:0||de */ QuickStatements 3.0"
    ))
    assert out.action is EventAction.DIRTY


def test_mul_language_is_kept() -> None:
    # The Wikidata "multilingual" / language-neutral bucket.
    assert classify(_edit("/* wbsetlabel-add:1|mul */ Foo")).action \
        is EventAction.DIRTY


def test_regional_lang_subtag_compares_as_a_whole() -> None:
    # en-gb is not literally "en" — we accept whole-tag matches only.
    # If we ever start emitting en-gb labels and want them, we'd have
    # to add it explicitly. Today, en-gb gets ignored.
    out = classify(_edit("/* wbsetlabel-set:1|en-gb */ Aluminium"))
    assert out.action is EventAction.IGNORE


def test_lang_scoped_kind_with_malformed_comment_fails_open() -> None:
    # If we recognise the kind but can't parse a lang, KEEP — the
    # cost of an unneeded fetch is bounded, the cost of dropping a
    # real edit is silent staleness.
    out = classify(_edit("/* wbsetlabel-add */ broken"))
    assert out.action is EventAction.DIRTY


# ---------------------------------------------------------------- statement


def test_statement_create_is_dirty() -> None:
    assert classify(_edit("/* wbcreateclaim-create:1|P31 */ ...")).action \
        is EventAction.DIRTY


def test_statement_remove_is_dirty() -> None:
    assert classify(_edit("/* wbremoveclaims-remove:1| */ ...")).action \
        is EventAction.DIRTY


def test_reference_add_is_dirty() -> None:
    assert classify(_edit("/* wbsetreference-add */ ")).action \
        is EventAction.DIRTY


def test_redirect_create_is_dirty() -> None:
    # Redirects matter: the entity is being merged into another.
    # The worker handles the redirect-follow.
    assert classify(_edit("/* wbcreateredirect:0||Q42 */ ")).action \
        is EventAction.DIRTY


def test_merge_to_is_dirty() -> None:
    assert classify(_edit("/* wbmergeitems-to:0||Q9999 */ ")).action \
        is EventAction.DIRTY


def test_bulk_entity_update_is_dirty() -> None:
    # `wbeditentity-update` is the most common kind by volume and has
    # no language affordance — could be anything. Always KEEP.
    assert classify(_edit("/* wbeditentity-update:0| */ ...")).action \
        is EventAction.DIRTY


def test_empty_comment_is_dirty() -> None:
    # 21k events in the buffer had no comment at all. Fail open.
    assert classify(_edit("")).action is EventAction.DIRTY
    assert classify(_edit(None)).action is EventAction.DIRTY  # type: ignore[arg-type]


def test_unknown_kind_is_dirty() -> None:
    # Some future Wikidata RPC that doesn't exist today must not be
    # silently dropped.
    assert classify(_edit("/* wbsetfuturething-create */ ...")).action \
        is EventAction.DIRTY


def test_new_edit_type_is_dirty() -> None:
    # `type=new` (entity creation) — must KEEP.
    ev = {"type": "new", "wiki": "wikidatawiki",
          "title": "Q9999", "comment": "/* wbeditentity-create-item */ "}
    assert classify(ev).action is EventAction.DIRTY
