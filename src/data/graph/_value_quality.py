"""Shared Cypher fragments for contract value aggregation.

Two concerns are folded in here so every value aggregate across the API
handles them identically:

1. **Confidence gate.** The TED loader's confidence scorer marks
   implausible / internally-inconsistent contract values with
   ``value_low_confidence = true`` while keeping the node (see
   ``etl.contract_confidence``). Flagged rows contribute 0 so one impossible
   figure (e.g. the Greek EUR 1.07T digital-transformation typo) never
   distorts a country / region / sector total — the contract stays COUNTED,
   only its money is dropped.

2. **Modification collapse.** TED publishes each contract *modification*
   (``notice_type = 'can-modif'``) as its own :Contract node with its own
   ``AWARDED_TO`` edge and a *restated* (not incremental) value. Summing
   ``value_eur`` over a company's awards would count every restatement on top
   of the original award. The ``collapse_modifications`` ETL pass materialises
   ``current_value`` (the latest restated value) and ``is_current`` (exactly
   one canonical node per underlying contract). Aggregates therefore sum
   ``current_value`` over canonical nodes only.

Both flags are absent on rows written before their producers shipped, so they
coalesce safely: ``value_low_confidence`` → false (trust old rows), and a node
with no ``is_current`` is treated as canonical unless it is a ``can-modif``
notice (so the ~4/5 of never-modified contracts need no backfill, while raw
modification notices are excluded until the collapse pass stamps them).
"""
from __future__ import annotations


def canonical_predicate(binding: str = "ct") -> str:
    """Boolean Cypher: is this the single canonical node for its contract?

    True for the collapse-pass canonical node (``is_current = true``), and —
    when ``is_current`` is absent — for any node that is not itself a raw
    modification notice. Superseded modifications are excluded.
    """
    return (
        f"coalesce({binding}.is_current, "
        f"({binding}.notice_type IS NULL OR {binding}.notice_type <> 'can-modif'))"
    )


def _current_value(binding: str, *, cast: bool) -> str:
    """The value to sum for a canonical contract: the collapsed
    ``current_value`` when present, else the raw ``value_eur``."""
    inner = f"coalesce({binding}.current_value, {binding}.value_eur)"
    return f"toFloat({inner})" if cast else f"coalesce({inner}, 0)"


def trusted_value_sum(binding: str = "ct", *, cast: bool = False) -> str:
    """A Cypher ``sum()`` over contract values that (a) contributes 0 for
    confidence-flagged rows and (b) counts each underlying contract once by
    summing ``current_value`` over canonical nodes only (superseded
    modification restatements contribute 0).

    ``binding`` is the Contract variable in the surrounding MATCH. ``cast``
    wraps the value in ``toFloat`` for graphs where value_eur is stored as a
    string.
    """
    return (
        f"sum(CASE WHEN {canonical_predicate(binding)} "
        f"AND NOT coalesce({binding}.value_low_confidence, false) "
        f"THEN {_current_value(binding, cast=cast)} ELSE 0 END)"
    )


def canonical_count(binding: str = "ct") -> str:
    """A Cypher expression counting distinct underlying contracts: 1 per
    canonical node, 0 for superseded modification restatements. Drop-in
    replacement for ``count(ct)`` where a contract count is intended."""
    return f"sum(CASE WHEN {canonical_predicate(binding)} THEN 1 ELSE 0 END)"
