"""Reasoning service — runs business rules over the knowledge graph.

See the Reasoner book in BookStack (Architecture shelf) for the
architectural overview. Quick orientation:

- ``rule.py`` defines the ``Rule`` protocol and ``Finding`` dataclass.
- ``engine.py`` iterates rules and dispatches findings (auto-apply vs
  persist based on per-rule confidence thresholds).
- ``persistence.py`` writes findings and audit rows to Postgres.
- ``registry.py`` discovers and lists rules for the CLI.
- ``cli.py`` is the entrypoint (``python -m src.reasoner.cli``).
- ``rules/*`` are individual rules; each is its own module, each
  well-documented and well-tested.

Orchestration (CronJob, event-driven triggers) lives outside this
package — we only expose a CLI + rule registry.
"""
