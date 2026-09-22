"""Per-run accounting of what the stage did: totals, per rule, per
country, per publication year, and the first N examples of each rule.

Pure accumulation — the loader decides whether to log the summary,
write the JSON (``--report``) or print it (``--dry-run``).
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .outcomes import Quarantine, Rescale
from .stage import CleaningResult

DEFAULT_MAX_EXAMPLES = 20


class CleaningReport:  # pylint: disable=too-many-instance-attributes
    """Counters the loader feeds one ``CleaningResult`` at a time."""

    def __init__(self, max_examples: int = DEFAULT_MAX_EXAMPLES) -> None:
        self.max_examples = max_examples
        self.notices = 0
        self.notices_with_rules = 0
        self.suppliers_withheld = 0
        self.identifiers_normalised = 0
        self.values_rescaled = 0
        self.values_quarantined = 0
        self.by_rule: Counter = Counter()
        self.by_country: dict[str, Counter] = defaultdict(Counter)
        self.by_year: dict[str, Counter] = defaultdict(Counter)
        self.examples: dict[str, list[dict]] = defaultdict(list)

    def record(self, *, notice_id: str, country: str | None,
               year: str | None, result: CleaningResult) -> None:
        self.notices += 1
        if not result.fired:
            return
        self.notices_with_rules += 1
        self.suppliers_withheld += len(result.withheld)
        self.identifiers_normalised += result.counters.get(
            "generic.national_id_country_prefixed", 0)
        if isinstance(result.value, Rescale):
            self.values_rescaled += 1
        elif isinstance(result.value, Quarantine):
            self.values_quarantined += 1
        country_key, year_key = country or "?", year or "?"
        for rule_id, n in result.counters.items():
            self.by_rule[rule_id] += n
            self.by_country[country_key][rule_id] += n
            self.by_year[year_key][rule_id] += n
        for outcome in result.outcomes:
            bucket = self.examples[outcome.rule_id]
            if len(bucket) < self.max_examples:
                bucket.append({"notice_id": notice_id, **outcome.example()})

    def as_dict(self) -> dict:
        return {
            "totals": {
                "notices": self.notices,
                "notices_with_rules": self.notices_with_rules,
                "suppliers_withheld": self.suppliers_withheld,
                "identifiers_normalised": self.identifiers_normalised,
                "values_rescaled": self.values_rescaled,
                "values_quarantined": self.values_quarantined,
            },
            "by_rule": dict(sorted(self.by_rule.items())),
            "by_country": {
                c: dict(sorted(v.items())) for c, v in sorted(self.by_country.items())
            },
            "by_year": {
                y: dict(sorted(v.items())) for y, v in sorted(self.by_year.items())
            },
            "examples": {
                r: list(v) for r, v in sorted(self.examples.items())
            },
        }

    def summary_line(self) -> str:
        rules = ", ".join(f"{r}={n}" for r, n in sorted(self.by_rule.items()))
        return (
            f"cleaning: {self.notices} notices, {self.notices_with_rules} with "
            f"rules, {self.suppliers_withheld} suppliers withheld, "
            f"{self.identifiers_normalised} identifiers normalised, "
            f"{self.values_rescaled} values rescaled, "
            f"{self.values_quarantined} quarantined"
            + (f" [{rules}]" if rules else "")
        )
