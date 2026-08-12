"""Platform documentation, served to people and to the assistant.

Two consumers, one source. The help page renders these for a reader; the
assistant retrieves them to answer "how do I…" without the manual riding
along in every prompt.

They live here rather than in the community API because this is where the
agent-tool registry is: annotating a route with `agent_tool` is all it takes
for the assistant to be offered it, and a second mechanism for the same job
would be one more thing to keep in step.

Public on purpose — the articles describe how to use the product and contain
nothing user-specific, and requiring a session would stop the help page
working for the visitor most likely to need it.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src import docs
from src.api.agent_tools import agent_tool

# NOT "/docs": FastAPI serves the Swagger UI there, and it wins. Mounting
# here returned 200 with a page of Swagger HTML — a broken endpoint that
# looks healthy from every angle except reading the body. "/help" also
# matches the product's own name for this page.
router = APIRouter(prefix="/help", tags=["docs"])


@router.get(
    "",
    openapi_extra=agent_tool(
        name="list_docs",
        when=(
            "List the platform's documentation articles — ids, titles and "
            "one-line summaries, grouped by section. Call this when the user "
            "asks how a feature works, or before answering a 'how do I' "
            "question, then read the matching one with get_doc. Covers the "
            "Data Studio and the query language and schema of each data "
            "store."
        ),
        group="docs",
        core=True,
    ),
)
async def list_docs() -> dict:
    """Article metadata, without bodies.

    Bodies are omitted deliberately: the whole set is ~14k characters, which
    would spend most of a turn's tool budget before the model has chosen
    what it needs. The summaries are what it chooses from.
    """
    articles = docs.all_articles()
    sections: dict[str, list[dict]] = {}
    for article in articles:
        sections.setdefault(article.section, []).append(article.as_dict())
    return {"count": len(articles), "sections": sections}


@router.get(
    "/{article_id}",
    # Declared rather than left implicit: this spec is what generates the
    # agent tool, so an undocumented failure mode is one the model cannot
    # be told about either.
    responses={404: {"description": "No article with that id; the detail "
                                    "lists the ids that do exist."}},
    openapi_extra=agent_tool(
        name="get_doc",
        when=(
            "Read one documentation article in full, by an id from "
            "list_docs. Use it before explaining how to build a query or a "
            "plot, and before writing a query against a store whose schema "
            "you have not read this turn."
        ),
        group="docs",
        params=("article_id",),
    ),
)
async def get_doc(article_id: str) -> dict:
    article = docs.get_article(article_id)
    if article is None:
        # Names the alternatives rather than just refusing: the caller is
        # often a model that guessed an id, and a bare 404 sends it guessing
        # again.
        known = ", ".join(a.id for a in docs.all_articles())
        raise HTTPException(
            status_code=404,
            detail=f"No article '{article_id}'. Available: {known}",
        )
    return article.as_dict(with_body=True)
