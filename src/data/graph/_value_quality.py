"""Shared Cypher fragment for confidence-gated contract value sums.

The TED loader's confidence scorer marks implausible / internally-
inconsistent contract values with ``value_low_confidence = true`` while
keeping the node (see ``etl.contract_confidence``). Every value aggregate
across the API must exclude those so one impossible figure (e.g. the
Greek EUR 1.07T digital-transformation typo) never distorts a country /
region / sector total — but the contract stays COUNTED, so only its money
is dropped. The flag is absent on rows written before the scorer shipped,
so it coalesces to false (trust them; the old guards already passed
them).
"""
from __future__ import annotations


def trusted_value_sum(binding: str = "ct", *, cast: bool = False) -> str:
    """A Cypher ``sum()`` over contract values that contributes 0 for
    confidence-flagged rows.

    ``binding`` is the Contract variable in the surrounding MATCH.
    ``cast`` wraps the value in ``toFloat`` for graphs where value_eur may
    be stored as a string.
    """
    value = (
        f"toFloat({binding}.value_eur)"
        if cast else
        f"coalesce({binding}.value_eur, 0)"
    )
    return (
        f"sum(CASE WHEN coalesce({binding}.value_low_confidence, false) "
        f"THEN 0 ELSE {value} END)"
    )
