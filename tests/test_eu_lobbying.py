"""What the EU Transparency Register loader publishes, run as the cronjob
runs it: a register file in, the events the platform receives out.

The fixture is the register's real shape (XML 1.1, namespaced, control
characters escaped) with synthetic registrants: a company with its own
interests, an NGO, a consultancy and a self-employed individual.
"""
from __future__ import annotations

import gzip
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.etl import load_eu_lobbying

REGISTER = (Path(__file__).parent / "fixtures" / "eu_lobbying" / "register.xml").read_bytes()


def _publish(xml: bytes = REGISTER) -> dict[str, dict]:
    """Run the loader on ``xml``; the UpsertDisclosure payloads by registrant."""
    log, emit = MagicMock(), MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    with patch.object(load_eu_lobbying, "resolve_entity", return_value=None):
        load_eu_lobbying.load_eu_lobbying(log, xml)
    return {c.kwargs["payload"]["disclosure_id"]: c.kwargs["payload"]
            for c in emit.upsert.call_args_list}


@pytest.fixture(name="published", scope="module")
def _published():
    return _publish()


def test_every_registrant_in_the_file_is_published(published):
    assert set(published) == {"763743132433-49", "9218245390-27", "32689998126-75",
                              "111222333444-55"}
    assert all(p["system"] == "eu-lobbying" for p in published.values())


def test_a_registrants_link_opens_its_register_entry_not_its_website(published):
    """Regression: `url` held the organisation's own site, so the page's
    "EU Transparency Register entry" link opened that site instead."""
    p = published["763743132433-49"]
    assert p["url"] == ("https://transparency-register.europa.eu/search-register-or-update/"
                        "organisation-detail_en?id=763743132433-49")
    assert p["details"]["website"] == "https://www.example-trading.test"


def test_goals_arrive_in_full(published):
    """They were cut at 500 characters: 65% of registrants lost the rest."""
    goals = published["9218245390-27"]["details"]["goals"]
    assert len(goals) > 1000 and goals.endswith("Steuerpolitik.")


def test_an_email_address_in_free_text_is_removed(published):
    assert published["763743132433-49"]["details"]["goals"] == (
        "A global trading firm. Questions: [e-mail removed].")


def test_a_count_of_zero_is_published_as_zero(published):
    """A registrant whose accredited persons drop to 0 must not keep the old
    count: zeros used to be left out, and the sink kept what it had."""
    details = published["763743132433-49"]["details"]
    assert details["ep_passes"] == 0
    assert details["members_fte"] == 0.3            # float noise rounded
    assert details["persons_involved"] == 3 and details["persons_10pct"] == 3


def test_countries_come_as_alpha3_like_company_nodes(published):
    by = {k: p["details"]["country_iso"] for k, p in published.items()}
    assert by == {"763743132433-49": "USA", "9218245390-27": "DEU",
                  "32689998126-75": "TUR", "111222333444-55": "BEL"}


def test_a_companys_spend_band_and_intermediaries_are_kept(published):
    d = published["763743132433-49"]["details"]
    assert (d["financial_type"], d["cost_min"], d["cost_max"]) == ("own_interests", 10000, 24999)
    assert (d["financial_year_start"], d["financial_year_end"]) == ("2025-01-01", "2025-12-01")
    assert d["intermediary_names"] == ["Example Advisers"]
    assert (d["intermediary_cost_min"], d["intermediary_cost_max"]) == ([10000], [24999])
    assert d["intermediary_names_current"] == ["Example Advisers"]
    assert d["eu_legislative_proposals"].startswith("Financial services regulation")
    assert d["levels_of_interest"] == ["european", "global"]
    assert d["eu_office_city"] == "London" and d["eu_office_country"] == "UNITED KINGDOM"
    assert "ep_intergroups" not in d                 # "N/A" is no grouping


def test_an_ngos_budget_funding_and_grants_are_kept(published):
    """32% of registrants that declare money do it here, and none of it
    reached the platform."""
    d = published["9218245390-27"]["details"]
    assert d["financial_type"] == "ngo" and d["total_budget_eur"] == 1260031
    assert d["funding_sources"] == ["Member's contributions", "EU funding"]
    assert d["contributor_names"] == ["Member breweries", "Foundation Example"]
    assert d["contributor_amounts_eur"] == [820000, 0]
    assert (d["grant_sources"], d["grant_amounts_eur"]) == (["EU LIFE"], [82484])
    assert (d["grant_sources_current"], d["grant_amounts_eur_current"]) == (["EU LIFE"], [62000])
    assert d["ep_intergroups"] == ["Beer Club", "SME Intergroup"]
    assert d["member_of"] == "The Brewers of Europe"
    assert d["communication_activities"] == "Positionspapiere  und Konsultationsbeiträge."
    assert d["financial_complementary_info"] == "Wir sind gemeinnützig."


def test_a_consultancys_revenue_and_clients_are_kept(published):
    d = published["32689998126-75"]["details"]
    assert (d["financial_type"], d["revenue_min"], d["revenue_max"]) == (
        "consultancy", 1000000, 1249999)
    assert d["client_names"] == ["Example Retail Holding", "Example Foods"]
    assert d["client_proposals"] == ["Packaging and Packaging Waste Regulation", "Novel foods"]
    assert (d["client_revenue_min"], d["client_revenue_max"]) == ([0, 10000], [10000, 24999])
    assert d["client_names_current"] == ["Example Retail Holding"]
    assert "cost_min" not in d                        # a consultancy declares no spend band


def test_a_self_employed_individual_keeps_no_postal_details(published):
    d = published["111222333444-55"]["details"]
    assert d["city"] == "Brussels" and "postcode" not in d
    assert d["goals"] == "Independent adviser on rural development."


def test_a_registrant_who_leaves_has_its_names_blanked_and_its_record_kept():
    log, emit = MagicMock(), MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    load_eu_lobbying.emit_deregistrations(log, {"9218245390-27"}, "2026-10-09")
    d = emit.upsert.call_args.kwargs["payload"]["details"]
    assert d["active"] is False and d["name"] == "[deregistered]"
    for named in ("contributor_names", "client_names", "intermediary_names"):
        assert d[named] == ["[deregistered]"]
    assert "goals" not in d and "interests" not in d          # the record stays


def test_a_snapshot_replays_to_the_same_events(tmp_path, monkeypatch):
    """Artifact first: the day's file is kept, and --file replays it."""
    path = load_eu_lobbying.write_artifact(REGISTER, str(tmp_path))
    assert Path(path).name.startswith("tr-") and Path(path).with_suffix("").exists() is False
    assert (tmp_path / Path(path).name.replace(".xml.gz", ".manifest.json")).exists()
    seen = {}
    monkeypatch.setattr(load_eu_lobbying.EventLog, "from_env", classmethod(lambda cls: MagicMock()))
    monkeypatch.setattr(load_eu_lobbying, "load_eu_lobbying",
                        lambda _log, xml: seen.setdefault("xml", xml))
    load_eu_lobbying.main(["--file", path])
    assert seen["xml"] == REGISTER == gzip.decompress(Path(path).read_bytes())
