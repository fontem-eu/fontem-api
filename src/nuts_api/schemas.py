"""Response models for the public NUTS reference API.

Shapes are part of the contract other people's code will depend on, so they
are declared rather than assembled ad hoc in the routers.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Source(BaseModel):
    id: str = Field(description="Short id, as it appears in a region's `sources`")
    name: str
    gives: str = Field(description="What this source contributes")
    licence: str
    attribution: str
    url: str


class Coverage(BaseModel):
    regions: int
    with_translations: int
    average_languages: float
    per_language: dict[str, int]
    per_level: dict[str, dict[str, int]]


class ServiceInfo(BaseModel):
    """What this API is, where its names come from, and how complete it is."""

    nuts_version: str = Field(description="The NUTS vintage the codes belong to")
    generated: str = Field(description="ISO date the gazetteer was last built")
    languages: list[str] = Field(description="The 24 official EU languages")
    coverage: Coverage
    sources: list[Source]
    licence: str
    endpoints: dict[str, str]


class Region(BaseModel):
    code: str
    level: int
    country: str = Field(description="NUTS-0 code of the country this sits in")
    parent: str | None = Field(description="Parent NUTS code, null at level 0")
    name_native: str = Field(description="Eurostat's national-language name")
    name_latn: str = Field(description="Eurostat's Latin transliteration")
    names: dict[str, str] = Field(
        description="Name per language, for the languages a name is known in")
    aliases: list[str] = Field(description="Other forms the region answers to")
    sources: list[str] = Field(
        description="Which sources this record's names came from")


class RegionDetail(Region):
    ancestors: list[Region] = Field(description="Parent chain, closest first")
    children: list[Region] = Field(description="Direct children, code order")


class RegionPage(BaseModel):
    total: int = Field(description="Matching regions before limit/offset")
    limit: int
    offset: int
    regions: list[Region]


class Match(BaseModel):
    """One search hit, and what in it matched."""

    code: str
    level: int
    country: str
    name: str = Field(description="Name in the requested display language")
    name_native: str
    matched: str = Field(description="The form that matched the query")
    matched_language: str | None = Field(
        description="Language of the matched form; null for a code or a "
                    "language-less alias")
    matched_kind: str = Field(description="code | name | native | latn | alias")
    rank: int = Field(description="0 is the strongest match; ties keep code order")


class SearchResults(BaseModel):
    query: str
    lang: str
    total: int
    matches: list[Match]
