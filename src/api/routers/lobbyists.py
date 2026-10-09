"""
Lobbyists API Router
====================
The EU Transparency Register entry for one organisation.

A lobbying declaration is a thing in its own right, not a footnote on a
company page: only 3,565 of 18,195 registrants resolve to a company we
hold, so routing these through /company/ left roughly four in five with
nowhere to go.

`disclosure_id` is the key because it is the only identifier these nodes
carry. `gmr_id`, `tr_id` and `transparency_register_id` are each present
on ZERO Lobbyist nodes, despite graph.py and mentions.py having
referenced the latter two.
"""
from __future__ import annotations

from typing import Annotated, Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query

from src.api.lang import safe_lang
from src.data.graph.neo4j_client import Neo4jClient


router = APIRouter(prefix="/lobbyists", tags=["lobbyists"])


def _money(node: dict[str, Any]) -> dict[str, Any] | None:
    """The declared annual lobbying spend, as the register states it.

    The register collects a BAND, not a figure, and a band with only one
    end is still information ("at least 10M") — so this returns whatever
    is present rather than requiring both.
    """
    low, high = node.get("detail_cost_min"), node.get("detail_cost_max")
    if low is None and high is None:
        return None
    return {"min_eur": low, "max_eur": high, "currency": "EUR"}


#: A registrant's page on the register. The node's `url` held the
#: organisation's own website until the 2026-10 re-ingest; the id is the
#: one thing that names the register entry, whatever `url` holds.
REGISTER_PAGE = ("https://transparency-register.europa.eu/search-register-or-update/"
                 "organisation-detail_en?id={disclosure_id}")


def _in_language(node: dict[str, Any], lang: str | None) -> dict[str, Any]:
    """The goals and their summary in the reader's language, where a
    translation of the goals as they read now exists; else as written.
    A translation or summary made from goals since rewritten is not shown."""
    goals = node.get("detail_goals")
    source = node.get("detail_goals_lang")
    translated = None
    if lang and lang != source and node.get("detail_goals_translated_from") == goals:
        translated = node.get(f"detail_goals_{lang}")
    summary = None
    if node.get("detail_goals_summarized_from") == goals:
        summary = node.get(f"detail_goals_summary_{lang}") if lang else None
        summary = summary or node.get(f"detail_goals_summary_{source}")
    return {
        "goals": translated or goals,
        "goals_original": goals,
        "goals_lang": source,
        "goals_translated": translated is not None,
        "goals_summary": summary,
    }


def _band(node: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    low, high = node.get(f"detail_{prefix}_min"), node.get(f"detail_{prefix}_max")
    if low is None and high is None:
        return None
    return {"min_eur": low, "max_eur": high, "currency": "EUR"}


def _rows(node: dict[str, Any], **columns: str) -> list[dict[str, Any]]:
    """Parallel detail_* lists as rows: _rows(node, name="client_names", ...)."""
    lists = {k: node.get(f"detail_{v}") or [] for k, v in columns.items()}
    n = max((len(v) for v in lists.values()), default=0)
    return [{k: (v[i] if i < len(v) else None) for k, v in lists.items()} for i in range(n)]


def _finances(node: dict[str, Any]) -> dict[str, Any]:
    """The financial year as the registrant declared it, whatever kind of
    registrant it is: amounts as declared, unaudited."""
    clients = _rows(node, name="client_names", proposal="client_proposals",
                    revenue_min_eur="client_revenue_min", revenue_max_eur="client_revenue_max")
    intermediaries = _rows(node, name="intermediary_names",
                           cost_min_eur="intermediary_cost_min",
                           cost_max_eur="intermediary_cost_max")
    return {
        "type": node.get("detail_financial_type"),
        "year_start": node.get("detail_financial_year_start"),
        "year_end": node.get("detail_financial_year_end"),
        "total_budget_eur": node.get("detail_total_budget_eur"),
        "revenue": _band(node, "revenue"),
        "funding_sources": node.get("detail_funding_sources"),
        "contributors": _rows(node, name="contributor_names",
                              amount_eur="contributor_amounts_eur"),
        "clients": clients,
        "intermediaries": intermediaries,
        "grants": _rows(node, source="grant_sources", amount_eur="grant_amounts_eur"),
        "grants_current_year": _rows(node, source="grant_sources_current",
                                     amount_eur="grant_amounts_eur_current"),
        "complementary_information": node.get("detail_financial_complementary_info"),
    }


def _profile(node: dict[str, Any], filed_for: list[dict],
             lang: str | None = None) -> dict[str, Any]:
    return {
        "disclosure_id": node.get("disclosure_id"),
        "name": node.get("detail_name"),
        "name_latin": node.get("detail_name_latin"),
        "acronym": node.get("detail_acronym"),
        "category": node.get("detail_category"),
        "entity_form": node.get("detail_entity_form"),
        "interest_represented": node.get("detail_interest_represented"),
        "country": node.get("detail_country"),
        "country_iso": node.get("detail_country_iso"),
        "city": node.get("detail_city"),
        "eu_office": ({"city": node.get("detail_eu_office_city"),
                       "country": node.get("detail_eu_office_country")}
                      if node.get("detail_eu_office_city") else None),
        "website": node.get("detail_website"),
        **_in_language(node, lang),
        "interests": node.get("detail_interests"),
        "levels_of_interest": node.get("detail_levels_of_interest"),
        "eu_legislative_proposals": node.get("detail_eu_legislative_proposals"),
        "communication_activities": node.get("detail_communication_activities"),
        "eu_forums_platforms": node.get("detail_eu_forums_platforms"),
        "ep_intergroups": node.get("detail_ep_intergroups"),
        "member_of": node.get("detail_member_of"),
        "organisation_members": node.get("detail_organisation_members"),
        "declared_spend": _money(node),
        "finances": _finances(node),
        "members_fte": node.get("detail_members_fte"),
        "persons_involved": node.get("detail_persons_involved"),
        "ep_passes": node.get("detail_ep_passes"),
        "registered_on": node.get("detail_registration_date"),
        "last_updated": node.get("detail_last_updated"),
        "active": node.get("detail_active"),
        # The register's own page for this registrant. Kept distinct
        # from `website`, which is the organisation's own site.
        "register_url": REGISTER_PAGE.format(disclosure_id=node.get("disclosure_id")),
        # Entities that filed this declaration. Empty for most, which is
        # the honest answer rather than a reason to hide the page.
        "filed_for": filed_for,
    }


@router.get(
    "/{disclosure_id}",
    responses={
        404: {"description": "no register entry with that disclosure_id"},
    },
)
@inject
def lobbyist_detail(
    disclosure_id: str,
    lang: Annotated[str | None, Query(max_length=10)] = None,
    *,
    neo4j: FromDishka[Neo4jClient],
) -> dict[str, Any]:
    """One registrant, plus whoever filed the declaration. With ``lang``,
    the goals and their summary in that language where translated, the
    original beside them."""
    with neo4j.session() as session:
        # Single indexed seek on disclosure_id (see graph_schema). One
        # property, no OR across several — a disjunction over different
        # properties cannot use the index and label-scans instead.
        record = session.run(
            "MATCH (l:Lobbyist {disclosure_id: $did}) "
            "OPTIONAL MATCH (l)-[:FILED_BY]->(e) "
            "RETURN l AS lobbyist, "
            "  collect({label: labels(e)[0], name: e.name, "
            "           gmr_id: e.gmr_id}) AS filed_for "
            "LIMIT 1",
            did=disclosure_id,
        ).single()
        if not record or record["lobbyist"] is None:
            raise HTTPException(status_code=404, detail="lobbyist not found")
        node = dict(record["lobbyist"])
        # OPTIONAL MATCH yields one all-null row when nothing matched;
        # a filer without a name is not one we can link to anyway.
        filed_for = [
            {
                "label": e["label"],
                "name": e["name"],
                # The profile route only exists for entities that have a
                # gmr_id; without one there is nowhere to send a reader.
                "profile": f"/company/{e['gmr_id']}"
                           if e["label"] == "Company" and e.get("gmr_id") else None,
            }
            for e in (record["filed_for"] or [])
            if e and e.get("name")
        ]
    return _profile(node, filed_for, safe_lang(lang))
