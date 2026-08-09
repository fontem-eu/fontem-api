"""Mark an endpoint as callable by the assistant, and generate its schema.

The assistant reached 4 of this API's 84 read-only endpoints. Everything it
could not reach, it denied having — which is how a platform with eight
population datasets told a user it holds "only procurement".

Hand-writing tool schemas would reproduce the drift problem one layer up: a
parameter renamed in a route signature and not in a hand-copied schema
produces a tool that fails at call time with no compile-time signal. So the
schema is derived from the route itself. FastAPI already knows the path, the
parameters, their types and their descriptions; ``agent_tool`` adds only what
FastAPI cannot infer — whether a human decided this endpoint is worth the
model's attention, and what to call it.

Opt-in, never automatic. Exposing all 84 would cost roughly 20k tokens of
schema per turn and make selection worse: our own eval shows models
mis-selecting among four tools, and one called a *forbidden* tool. The
registry is allowed to be large because the per-turn surface is scoped
separately; the two numbers are not the same and should not be confused.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: OpenAPI vendor extension key. Lives in the schema so the generator needs
#: nothing but the served spec — no imports, no shared runtime.
AGENT_TOOL_KEY = "x-agent-tool"


@dataclass(frozen=True)
class AgentTool:
    """What a human decided about exposing this endpoint."""

    #: Tool name the model sees. Snake case, verb-first, no prefix — the
    #: caller adds any namespace it needs.
    name: str
    #: One line telling the model WHEN to reach for this, not what it
    #: returns. "when" beats "what": models pick tools by matching intent.
    when: str
    #: Coarse grouping used to scope the per-turn surface, so a turn about
    #: statistics is not offered eleven contract tools.
    group: str = "general"
    #: Parameters worth exposing. Empty means every query parameter the
    #: route declares. Naming a subset keeps schemas small and stops the
    #: model tuning knobs it has no basis to set.
    params: tuple[str, ...] = field(default_factory=tuple)
    #: Never scoped away. Core tools are the ones a turn cannot discover its
    #: way out of without: you cannot ask for a statistic without first
    #: finding its dataset code, so gating that discovery behind a group
    #: guess would strand the model exactly where it already fails — telling
    #: the user the data is not here. Keep this set small; every core tool is
    #: schema in every prompt.
    core: bool = False


def agent_tool(name: str, when: str, group: str = "general",
               params: tuple[str, ...] = (), core: bool = False) -> dict:
    """Return the ``openapi_extra`` payload marking a route as a tool.

    Usage::

        @router.get("/search", openapi_extra=agent_tool(
            name="search_entities",
            when="the user names a company, authority, person or lobbyist",
            group="entities", params=("q", "limit")))
    """
    return {AGENT_TOOL_KEY: {"name": name, "when": when, "group": group,
                             "params": list(params), "core": core}}
