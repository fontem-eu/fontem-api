"""Typed outcomes a rule can return, and the value decision the stage
folds them into.

Every outcome names the rule that produced it and a subject (an
organisation id, the value, a date field), plus ``example()`` — the
short record the dry-run report keeps as evidence for that rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .facts import ValueFacts


@dataclass(frozen=True)
class Outcome:
    """Base: something a rule decided about one subject of a notice."""

    rule_id: str
    subject: str

    def example(self) -> dict:
        """The report record: what was looked at and what was decided."""
        return {"subject": self.subject, "decision": self.decision()}

    def decision(self) -> str:
        return self.rule_id


@dataclass(frozen=True)
class SupplierWithheld(Outcome):
    """A supplier whose name is not a name: no entity is created."""

    name_raw: str
    role: str

    def example(self) -> dict:
        return {"org_id": self.subject, "name_raw": self.name_raw,
                "role": self.role, "decision": "withheld"}


@dataclass(frozen=True)
class IdentifierNormalised(Outcome):
    """A bare national id that became a canonical VAT once prefixed."""

    raw: str
    canonical: str

    def example(self) -> dict:
        return {"org_id": self.subject, "raw": self.raw,
                "decision": f"canonical {self.canonical}"}


@dataclass(frozen=True)
class ValueRescaled(Outcome):
    """A monetary scale error the existing tiers correct with a marker."""

    factor: float
    tier: str
    detail: str
    corrected: "ValueFacts"

    def example(self) -> dict:
        return {"subject": self.subject, "tier": self.tier,
                "decision": f"rescale x{self.factor:g}", "detail": self.detail}


@dataclass(frozen=True)
class ValueQuarantined(Outcome):
    """A value withheld for a human: implausible, correction undecidable."""

    reason: str
    detail: str
    value_eur: float
    candidates: tuple[float, ...]

    def example(self) -> dict:
        return {"subject": self.subject, "value_eur": self.value_eur,
                "decision": f"quarantine {self.reason}",
                "candidates": list(self.candidates), "detail": self.detail}


@dataclass(frozen=True)
class DatePlaceholder(Outcome):
    """A sentinel date the loader discards from the typed field."""

    raw: str

    def example(self) -> dict:
        return {"field": self.subject, "raw": self.raw,
                "decision": "placeholder dropped from typed field"}


# ── the folded value decision ──────────────────────────────────────


@dataclass(frozen=True)
class Keep:
    """Nothing to do to the value."""


@dataclass(frozen=True)
class Rescale:
    """Apply ``factor`` to every monetary field; ``reason`` is the tier
    that goes on ``value_scale_corrected``."""

    factor: float
    reason: str
    detail: str
    corrected: "ValueFacts"


@dataclass(frozen=True)
class Quarantine:
    """Withhold the value; ``reason`` goes on ``value_quarantine_reason``
    and ``candidates`` (possible corrected values) into the review note."""

    reason: str
    detail: str
    candidates: tuple[float, ...] = ()


ValueDecision = Keep | Rescale | Quarantine
