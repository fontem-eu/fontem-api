"""What a producer declares about the data it publishes.

Every ETL loader answers the same questions — what did you load, about what,
covering which period, from where — and until now nothing carried those
answers. The producer identity existed only as a string handed to
``log.batch(producer=...)``, and anything that wanted to describe the
platform's holdings had to be written by hand somewhere else and kept in
sync by memory.

That failed exactly the way hand-maintained descriptions do. Asked about
demographic data the assistant answered that Fontem holds "only procurement",
because the prose it had been given predated half the pipelines.

So the description lives next to the code that produces the data. A loader
that changes what it writes changes this constant in the same diff, in the
same file, reviewed together.

Read without importing
----------------------
The API process must never import a loader: they pull ``fontem_event_schemas``,
network clients and parsers that have no business inside a request handler.
``registry.py`` therefore reads these constants with ``ast``, never executing
the module. That is why every field here has to be a plain literal — no
computed values, no f-strings, no references to other names. The registry
rejects anything it cannot evaluate literally rather than guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DataDescription:  # pylint: disable=too-many-instance-attributes
    """One producer's public description of what it loads.

    Nine fields rather than seven, deliberately. Each one answers a question a
    reader actually asks — what, about what, how much of it, from where, how
    often — and collapsing any of them into a free-text blob would put the
    answer back in prose that nothing can check.

    Written for two readers who want different things: an assistant deciding
    whether the platform can answer a question, and a person reading the
    data-quality dashboard. Both are served by plain language, so ``summary``
    and ``answers`` should avoid pipeline vocabulary — the user asking about
    company ownership does not know what GLEIF is.
    """

    #: Matches ``log.batch(producer=...)`` exactly. The join key to etl_run
    #: rows and entity_events, so a typo here silently orphans the row.
    producer: str
    #: Human display name, e.g. "EU Sanctions".
    label: str
    #: Grouping slug shared with the dashboard: procurement, corporate,
    #: influence, securities, geography, climate, reference.
    theme: str
    #: One sentence, no jargon: what this data IS.
    summary: str
    #: Graph labels or record kinds this producer writes. Lets a reader map
    #: "Company" in the graph back to the feed responsible for it.
    entities: tuple[str, ...] = field(default_factory=tuple)
    #: What is and is not in scope, in the user's terms. The single most
    #: valuable field for an assistant: it is what stops "0 results" being
    #: reported as "absent from the world".
    coverage: str = ""
    #: Where it comes from upstream, named as the publisher would name it.
    upstream: str = ""
    #: Rough cadence, e.g. "daily", "monthly", "one-off".
    update_freq: str = ""
    #: Concrete questions this data can answer. These are what let a model
    #: route a vague request to the right feed, and they double as eval
    #: material — a question listed here should be answerable.
    answers: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "producer": self.producer,
            "label": self.label,
            "theme": self.theme,
            "summary": self.summary,
            "entities": list(self.entities),
            "coverage": self.coverage,
            "upstream": self.upstream,
            "update_freq": self.update_freq,
            "answers": list(self.answers),
        }
