"""Platform documentation, kept small and addressable.

Articles exist for two readers at once. A person opens them from the help
page; the assistant retrieves them to answer "how do I…" without the whole
manual being pasted into every turn.

That second reader is why they are deliberately short. The context window is
32k and a turn already carries a system prompt, a catalogue, thirteen tool
schemas and history — an article that arrives as a RAG hit has to earn its
tokens. One article is sized to be one retrievable chunk, so a hit is
self-contained and nothing has to be re-assembled from fragments.

Content lives as plain HTML files next to a manifest of metadata. No build
step, no database migration: adding an article is adding a file and a line.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

_HERE = pathlib.Path(__file__).parent
_ARTICLES = _HERE / "articles"
_MANIFEST = _HERE / "manifest.json"


@dataclass(frozen=True)
class Article:
    """One documentation article: metadata plus its HTML body."""
    id: str
    section: str
    title: str
    summary: str
    tags: tuple[str, ...]
    body: str = ""

    def as_dict(self, *, with_body: bool = False) -> dict:
        out = {"id": self.id, "section": self.section, "title": self.title,
               "summary": self.summary, "tags": list(self.tags)}
        if with_body:
            out["body"] = self.body
        return out

    @property
    def embedding_text(self) -> str:
        """What to embed for retrieval.

        Title and summary lead: a query like "how do I make a chart" matches
        the framing far better than it matches the middle of a code block,
        and prepending them means a chunk carries its own context even
        though the body follows.
        """
        return f"{self.title}\n{self.summary}\n\n{_strip_tags(self.body)}"


def _strip_tags(html: str) -> str:
    """Crude tag removal for the embedding text.

    Deliberately not a parser: the bodies are our own hand-written HTML with
    no attributes worth keeping, and a dependency for this would be a poor
    trade. Tag names never appear as prose in these files.
    """
    out, depth = [], 0
    for ch in html:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


def _load() -> list[Article]:
    manifest = json.loads(_MANIFEST.read_text("utf-8"))
    articles = []
    for row in manifest.get("articles", []):
        path = _ARTICLES / f"{row['id']}.html"
        articles.append(Article(
            id=row["id"], section=row["section"], title=row["title"],
            summary=row["summary"], tags=tuple(row.get("tags", ())),
            body=path.read_text("utf-8") if path.exists() else "",
        ))
    return articles


def all_articles() -> list[Article]:
    """Every article, manifest order. Read per call — there are eight of
    them, and a cache would only be a way to serve a stale one after a
    deploy."""
    return _load()


def get_article(article_id: str) -> Article | None:
    return next((a for a in all_articles() if a.id == article_id), None)


def missing_bodies() -> list[str]:
    """Manifest entries with no HTML file. A listing that advertises an
    article the reader cannot open is worse than one that omits it."""
    return [a.id for a in all_articles() if not a.body.strip()]
