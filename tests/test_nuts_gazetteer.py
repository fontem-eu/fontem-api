"""The multilingual NUTS gazetteer: how names are chosen, and how it is built.

Two halves. ``src.api.nuts_gazetteer`` reads the bundled artifact and decides
which of a region's names to show; ``src.etl.build_nuts_gazetteer`` builds
that artifact from Eurostat, the Publications Office and Wikidata. The
builder's network calls are not exercised here — its merge and normalisation
rules are, on fixtures, because those are what silently get a name wrong.
"""
from __future__ import annotations

# pylint: disable=missing-function-docstring

import json

from src.api import nuts_gazetteer
from src.etl import build_nuts_gazetteer as builder


# ── serving: folding ───────────────────────────────────────────


def test_fold_is_case_and_accent_insensitive():
    assert nuts_gazetteer.fold("Ática") == "atica"
    assert nuts_gazetteer.fold("Grande  Lisboa ") == "grande lisboa"
    assert nuts_gazetteer.fold("Łódzkie") == "łodzkie"


def test_fold_agrees_with_the_web_app_on_the_awkward_letters():
    """foldText() in the web app hard-codes these two substitutions to match
    casefold(); if this side ever changes, the picker stops matching its own
    index."""
    assert nuts_gazetteer.fold("Bergstraße") == "bergstrasse"
    assert nuts_gazetteer.fold("Περιφέρεια Αττικής") == "περιφερεια αττικησ"


# ── serving: language resolution ───────────────────────────────


def test_resolve_language_takes_the_primary_subtag():
    assert nuts_gazetteer.resolve_language("pt-BR") == "pt"
    assert nuts_gazetteer.resolve_language("EL") == "el"
    assert nuts_gazetteer.resolve_language("de_AT") == "de"


def test_resolve_language_falls_back_to_english():
    for raw in (None, "", "   ", "zz", "klingon", "x"):
        assert nuts_gazetteer.resolve_language(raw) == "en"


def test_the_bundled_gazetteer_covers_every_bundled_boundary():
    """The endpoint serves the list from the gazetteer, not from the boundary
    files, so a boundary file that gained regions without the gazetteer being
    rebuilt would silently drop them from the picker."""
    coded = nuts_gazetteer.document()["regions"]
    for level in range(4):
        path = f"src/api/data/nuts{level}.geojson"
        with open(path, encoding="utf-8") as handle:
            features = json.load(handle)["features"]
        codes = {(f.get("properties") or {}).get("nuts_code") for f in features}
        missing = {c for c in codes if c and c not in coded}
        assert not missing, f"nuts{level}.geojson has {missing} — rebuild the gazetteer"


def test_the_bundled_gazetteer_is_multilingual():
    """A guard against shipping an artifact built without network access to
    the label sources: it would load, serve, and quietly be Eurostat-only."""
    regions = nuts_gazetteer.document()["regions"]
    translated = [e for e in regions.values() if e["labels"]]
    assert len(translated) > len(regions) * 0.7
    assert len(nuts_gazetteer.languages()) == 24


# ── building: name normalisation ───────────────────────────────


def test_normalise_name_cleans_whitespace():
    assert builder.normalise_name("Αττική ") == "Αττική"
    assert builder.normalise_name("Paros,  Syros") == "Paros, Syros"
    assert builder.normalise_name(None) == ""


def test_normalise_name_folds_a_latin_letter_hiding_in_a_greek_name():
    """Eurostat spells EL30 with U+0041 LATIN CAPITAL A followed by Greek
    letters, so searching for it in Greek finds nothing."""
    assert builder.normalise_name("Aττική") == "Αττική"
    assert builder.normalise_name("Ανατολική")\
        == "Ανατολική"


def test_normalise_name_leaves_a_genuinely_latin_name_alone():
    assert builder.normalise_name("Bolzano/Bozen") == "Bolzano/Bozen"
    assert builder.normalise_name("Attiki") == "Attiki"


# ── building: merge rules ──────────────────────────────────────


REGIONS = {
    "EL": {"level": 0, "native": "Ελλάδα", "latn": "Ellada"},
    "EL3": {"level": 1, "native": "Αττική", "latn": "Attiki"},
    "EL30": {"level": 2, "native": "Αττική", "latn": "Attiki"},
    "PT1A0": {"level": 3, "native": "Grande Lisboa", "latn": "Grande Lisboa"},
}


def _build(**over):
    kwargs = {
        "country_labels": {"EL": {"en": "Greece", "el": "Ελλάδα"}},
        "wd_labels": {"EL": {"en": "Hellenic Republic"},
                      "EL3": {"en": "Attica Region", "fr": "périphérie d'Attique"},
                      "PT170": {"en": "Lisbon metropolitan area",
                                "fr": "Aire métropolitaine de Lisbonne"}},
        "wd_aliases": {"EL3": {"Attica", "Ática"}, "PT170": {"AML"}},
        "succession": {"PT1A0": {"PT170"}},
        "metro_labels": {},
    }
    kwargs.update(over)
    return builder.build_gazetteer(REGIONS, builder.Sources(**kwargs))


def test_build_keeps_both_eurostat_names_and_records_its_sources():
    entry = _build()["EL3"]
    assert (entry["native"], entry["latn"]) == ("Αττική", "Attiki")
    assert entry["labels"]["en"] == "Attica Region"
    assert entry["src"] == ["eurostat", "wikidata"]


def test_build_inherits_labels_from_the_code_a_region_replaced():
    """NUTS 2024 renumbered Lisbon's region; Wikidata still keys it by the
    old code, so the labels have to follow the succession chain or the
    region loses every translation it had."""
    entry = _build()["PT1A0"]
    assert entry["labels"]["fr"] == "Aire métropolitaine de Lisbonne"
    assert entry["src"] == ["eurostat", "wikidata:PT170"]


def test_build_prefers_the_official_country_name_over_wikidatas():
    """Country names are one of the few things the EU publishes in all 24
    languages, so there is no reason to guess at them."""
    entry = _build()["EL"]
    assert entry["labels"]["en"] == "Greece"
    assert "Hellenic Republic" in entry["aliases"]      # still searchable
    assert "euvoc" in entry["src"]


def test_build_adds_the_metro_region_as_an_alias():
    """Nobody searching for Athens types "Kentrikos Tomeas Athinon"."""
    entry = _build(metro_labels={"EL30": "Athina"})["EL30"]
    assert "Athina" in entry["aliases"]
    assert "euvoc" in entry["src"]


def test_build_borrows_from_a_parent_that_is_the_same_territory():
    """EL30 is the only NUTS 2 unit inside EL3 — same ground, two codes, and
    Wikidata carries an item for one of them."""
    entry = _build()["EL30"]
    assert entry["labels"]["en"] == "Attica Region"
    assert entry["src"] == ["eurostat", "wikidata:EL3"]


def test_build_drops_an_alias_that_repeats_a_name_it_already_has():
    entry = _build(wd_aliases={"EL3": {"Αττική", "Attiki", "Attica"}})["EL3"]
    assert entry["aliases"] == ["Attica"]


def test_build_orders_labels_by_language_not_by_arrival():
    entry = _build()["EL3"]
    assert list(entry["labels"]) == ["en", "fr"]


def test_predecessors_walks_the_chain_transitively():
    succession = {"C": {"B"}, "B": {"A"}}
    assert builder.predecessors("C", succession) == ["B", "A"]
    assert not builder.predecessors("A", succession)


def test_predecessors_survives_a_cycle():
    """A classification that says two codes replaced each other would
    otherwise hang the build."""
    assert sorted(builder.predecessors("X", {"X": {"Y"}, "Y": {"X"}})) == ["X", "Y"]


def test_coverage_report_counts_what_it_says_it_counts():
    report = builder.coverage_report(_build())
    assert report["regions"] == 4
    assert report["with_translations"] == 4
    assert report["per_level"][0] == {"regions": 1, "translated": 1}
    assert report["per_language"]["en"] == 4
    assert report["per_language"]["mt"] == 0
