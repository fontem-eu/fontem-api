"""Build the multilingual NUTS gazetteer served by ``GET /geo/nuts-regions``.

Why this exists
---------------
Eurostat publishes two names per NUTS region: the national-language one
(``NUTS_NAME``) and its Latin transliteration (``NAME_LATN``). Nothing else.
So a region picker fed from Eurostat alone is unsearchable for the 152
regions whose national name is written in Greek or Cyrillic — nobody finds
``Αττική`` by typing "Attica", and level 1+ has no English form at all.

There is no official 24-language translation of the NUTS names. This builds
the best defensible approximation, from three sources, each recorded per
region in ``src``:

``eurostat``   the vendored GISCO files (``data/nuts/polygons/``): the
               native name and the Latin transliteration, for every region.
               Always present; everything else is additive.
``euvoc``      the Publications Office *country* authority table, which is
               authoritative for the 24 official languages — but only covers
               level 0.
``wikidata``   labels + aliases in the 24 languages for whatever regions
               have an item carrying the NUTS code (P605).

Wikidata still keys many regions by their pre-2024 code (Lisbon's region is
``PT170`` there, ``PT1A0`` here), so codes with no item of their own inherit
the labels of the code they replaced, walking the official succession chain
from the Publications Office NUTS scheme. That is what recovers "Lisbonne",
"Lissabon" and "Lisbon" for ``PT1A0``.

Coverage is partial by nature — expect ~86% of regions to carry at least one
translation, averaging ~14 of the 24 languages, thinnest at level 3 and in
Maltese. The remainder fall back to the Latin transliteration, which is what
makes them searchable at all. Machine translation is deliberately not used:
it invents plausible-looking place names, and a wrong name in a transparency
tool is worse than an untranslated one.

Run it by hand when Eurostat publishes a new NUTS vintage, or to refresh the
Wikidata labels::

    python -m src.etl.build_nuts_gazetteer            # writes the JSON
    python -m src.etl.build_nuts_gazetteer --dry-run  # coverage report only

It needs egress to ``publications.europa.eu`` and the Wikidata mirror, which
the cluster does not have — hence a committed artifact rather than a runtime
lookup. The API reads the JSON and never talks to either service.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import pathlib
import sys
import unicodedata
import urllib.parse
import urllib.request

from src.services.location_service import LocationService

logger = logging.getLogger(__name__)

# The 24 official languages of the EU. The web app ships a locale file for
# each one, so these are exactly the languages a name can be displayed in.
LANGUAGES = (
    "bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "ga", "hr",
    "hu", "it", "lt", "lv", "mt", "nl", "pl", "pt", "ro", "sk", "sl", "sv",
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
POLYGONS_DIR = REPO_ROOT / "data" / "nuts" / "polygons"
BUNDLED_DIR = REPO_ROOT / "src" / "api" / "data"
OUTPUT_PATH = REPO_ROOT / "src" / "data" / "nuts" / "nuts_names.json"

EUVOC_SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
WIKIDATA_SPARQL = "https://qlever.dev/api/wikidata"
USER_AGENT = "fontem-nuts-gazetteer/1.0 (+https://fontem.eu; team@fontem.eu)"

# Latin letters that are visually identical to a Greek or Cyrillic one. They
# turn up *inside* otherwise-Greek names in Eurostat's own data (EL30
# "Aττική" and EL51 "Aνατολική..." both start with U+0041 LATIN CAPITAL A)
# and in the Publications Office's Bulgarian country labels. A name spelled
# with one is unsearchable in its own alphabet, so the confusable is folded
# back into the script the rest of the name is written in.
_CONFUSABLES = {
    "GREEK": {
        "A": "Α", "B": "Β", "E": "Ε", "H": "Η", "I": "Ι", "K": "Κ", "M": "Μ",
        "N": "Ν", "O": "Ο", "P": "Ρ", "T": "Τ", "X": "Χ", "Y": "Υ", "Z": "Ζ",
        "a": "α", "e": "ε", "o": "ο", "v": "ν", "x": "χ", "y": "γ",
    },
    "CYRILLIC": {
        "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І", "K": "К",
        "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
        "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    },
}


def _script_of(text: str) -> str | None:
    """Dominant script of a string, as the first word of its Unicode names."""
    counts: dict[str, int] = {}
    for char in text:
        if not char.isalpha():
            continue
        script = unicodedata.name(char, "?").split()[0]
        counts[script] = counts.get(script, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


def normalise_name(raw: str | None) -> str:
    """Clean one source name: whitespace, then confusable letters.

    Eurostat ships names with trailing spaces (``"Αττική "``) and doubled
    inner ones (``"Paros,  Syros"``); both defeat an exact match and look
    like sloppiness in the UI. The confusable pass runs second because it
    needs to know the dominant script of the cleaned name.
    """
    if not raw:
        return ""
    text = " ".join(raw.split())
    script = _script_of(text)
    table = _CONFUSABLES.get(script or "")
    if not table:
        return text
    # Only fold a Latin letter that is adjacent to a letter of the dominant
    # script — a genuinely mixed name ("Bolzano/Bozen" inside a Cyrillic
    # string, say) keeps its Latin run intact.
    chars = list(text)
    for i, char in enumerate(chars):
        if char not in table:
            continue
        neighbours = chars[max(0, i - 1):i] + chars[i + 1:i + 2]
        if any(_script_of(n) == script for n in neighbours):
            chars[i] = table[char]
    return "".join(chars)


def fold(text: str) -> str:
    """Case- and diacritic-insensitive form used for searching and dedup.

    ``Ática`` → ``atica``, ``Αττική`` → ``αττικη``. Mirrors ``foldText()``
    in the web app; the two must agree or a query that matches server-side
    ranking won't match client-side.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


def _sparql(endpoint: str, query: str, timeout: int = 180) -> list[dict]:
    body = urllib.parse.urlencode({"query": query}).encode()
    request = urllib.request.Request(
        endpoint, data=body,
        headers={"Accept": "application/sparql-results+json",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return payload["results"]["bindings"]


def read_eurostat_names(polygons_dir: pathlib.Path = POLYGONS_DIR,
                        version: str = "2024") -> dict[str, dict]:
    """Native + Latin names and levels, from the vendored GISCO files."""
    out: dict[str, dict] = {}
    for level in range(4):
        path = polygons_dir / f"NUTS_RG_10M_{version}_4326_LEVL_{level}.geojson"
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        for feature in data.get("features", []):
            props = feature.get("properties") or {}
            code = props.get("NUTS_ID")
            if not code:
                continue
            out[code] = {
                "level": props.get("LEVL_CODE", level),
                "native": normalise_name(props.get("NUTS_NAME")),
                "latn": normalise_name(props.get("NAME_LATN")),
            }
    return out


def read_bundled_extras(regions: dict[str, dict],
                        bundled_dir: pathlib.Path = BUNDLED_DIR) -> dict[str, dict]:
    """Add codes the API still serves boundaries for but the vintage dropped.

    ``UK`` is the live case: NUTS 2024 retired it, the bundled boundary files
    still carry it, and the platform still serves the UK. A code the picker
    can offer but the gazetteer has never heard of would lose its name
    entirely, so carry it over from the boundary file it comes from.
    """
    out = dict(regions)
    for level in range(4):
        path = bundled_dir / f"nuts{level}.geojson"
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        for feature in data.get("features", []):
            props = feature.get("properties") or {}
            code = props.get("nuts_code")
            if not code or code in out:
                continue
            name = normalise_name(props.get("name"))
            out[code] = {"level": props.get("level", level),
                         "native": name, "latn": name}
            logger.info("carried over retired code %s (%s)", code, name)
    return out


# NUTS-0 codes that are not the country's ISO alpha-2: Greece and the UK.
# Everything else maps through LocationService.
_NUTS0_TO_ALPHA3 = {"EL": "GRC", "UK": "GBR"}
_COUNTRY_CONCEPT = "http://publications.europa.eu/resource/authority/country/"


def fetch_country_labels(nuts0_codes) -> dict[str, dict[str, str]]:
    """Official country names per language, keyed by NUTS-0 code.

    The authority table is keyed by ISO alpha-3 and carries 43 languages,
    the 24 official ones among them. Asked for by explicit concept URI: the
    endpoint is a public shared Virtuoso and a query that has to scan the
    scheme to filter on a code times out.
    """
    wanted: dict[str, str] = {}
    for code in nuts0_codes:
        alpha3 = _NUTS0_TO_ALPHA3.get(code) or LocationService.alpha2_to_alpha3(code)
        if alpha3:
            wanted[alpha3] = code
    values = "\n".join(f"    <{_COUNTRY_CONCEPT}{a3}>" for a3 in sorted(wanted))
    rows = _sparql(EUVOC_SPARQL, f"""
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
    SELECT ?c ?lang ?label WHERE {{
      VALUES ?c {{
{values}
      }}
      ?c skos:prefLabel ?label .
      BIND(lang(?label) AS ?lang)
    }}
    """)
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        lang = row.get("lang", {}).get("value", "")
        if lang not in LANGUAGES:
            continue
        code = wanted.get(row["c"]["value"].rsplit("/", 1)[-1])
        if code:
            out.setdefault(code, {})[lang] = normalise_name(row["label"]["value"])
    return out


def fetch_succession() -> dict[str, set[str]]:
    """``code -> codes it replaced``, from the Publications Office NUTS scheme."""
    rows = _sparql(EUVOC_SPARQL, """
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
    PREFIX dct: <http://purl.org/dc/terms/>
    SELECT ?code ?oldcode WHERE {
      ?c skos:inScheme <http://data.europa.eu/nuts> ;
         skos:notation ?code ;
         dct:replaces ?old .
      ?old skos:notation ?oldcode .
    }
    """)
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(row["code"]["value"], set()).add(row["oldcode"]["value"])
    return out


def fetch_metro_labels() -> dict[str, str]:
    """Eurostat's metro-region name for the NUTS 3 units that have one.

    ``EL303`` is "Kentrikos Tomeas Athinon" in the classification and
    ``Athina`` as part of the Athens metro region — which is the word
    somebody looking for Athens actually types. 579 codes carry one.
    """
    rows = _sparql(EUVOC_SPARQL, """
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
    SELECT ?code ?metro WHERE {
      ?c skos:inScheme <http://data.europa.eu/nuts> ;
         skos:notation ?code ;
         <http://data.europa.eu/nuts/metroLabel> ?metro .
    }
    """)
    return {row["code"]["value"]: normalise_name(row["metro"]["value"])
            for row in rows}


def fetch_wikidata_names() -> tuple[dict[str, dict[str, str]], dict[str, set[str]]]:
    """Labels (per language) and aliases for every NUTS code Wikidata knows.

    Read from a Wikidata SPARQL mirror: the whole set is two queries there,
    against ~1.8k item lookups through the MediaWiki API.
    """
    langs = ",".join(f'"{code}"' for code in LANGUAGES)
    labels: dict[str, dict[str, str]] = {}
    for row in _sparql(WIKIDATA_SPARQL, f"""
    PREFIX wdt: <http://www.wikidata.org/prop/direct/>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT ?code ?lang ?label WHERE {{
      ?item wdt:P605 ?code .
      ?item rdfs:label ?label .
      BIND(LANG(?label) AS ?lang)
      FILTER(?lang IN ({langs}))
    }}
    """):
        code = row["code"]["value"]
        labels.setdefault(code, {})[row["lang"]["value"]] = \
            normalise_name(row["label"]["value"])

    aliases: dict[str, set[str]] = {}
    for row in _sparql(WIKIDATA_SPARQL, f"""
    PREFIX wdt: <http://www.wikidata.org/prop/direct/>
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
    SELECT ?code ?lang ?label WHERE {{
      ?item wdt:P605 ?code .
      ?item skos:altLabel ?label .
      BIND(LANG(?label) AS ?lang)
      FILTER(?lang IN ({langs}))
    }}
    """):
        aliases.setdefault(row["code"]["value"], set()).add(
            normalise_name(row["label"]["value"]))
    return labels, aliases


def predecessors(code: str, succession: dict[str, set[str]]) -> list[str]:
    """Codes this one replaced, oldest last, following the chain transitively."""
    seen: set[str] = set()
    queue = list(succession.get(code, ()))
    ordered: list[str] = []
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        ordered.append(current)
        queue.extend(succession.get(current, ()))
    return ordered


def _donor_codes(code: str, regions: dict[str, dict],
                 succession: dict[str, set[str]],
                 has_labels) -> list[str]:
    """Codes whose labels ``code`` may borrow, best first.

    Its own predecessors first (same territory, renumbered), then a parent
    or child that covers exactly the same ground — a NUTS 2 unit that is the
    only one in its NUTS 1 region is the same place under another code, and
    Wikidata often carries only one of the two.
    """
    donors = [c for c in predecessors(code, succession) if has_labels(c)]
    siblings = [c for c in regions if len(c) == len(code) + 1 and c.startswith(code)]
    if len(siblings) == 1 and has_labels(siblings[0]):
        donors.append(siblings[0])
    parent = code[:-1]
    if len(parent) >= 2 and parent in regions and has_labels(parent):
        cousins = [c for c in regions if len(c) == len(code) and c.startswith(parent)]
        if len(cousins) == 1:
            donors.append(parent)
    return donors


@dataclasses.dataclass(frozen=True)
class Sources:
    """Everything the merge reads, so one object travels instead of five."""

    country_labels: dict[str, dict[str, str]]
    wd_labels: dict[str, dict[str, str]]
    wd_aliases: dict[str, set[str]]
    succession: dict[str, set[str]]
    metro_labels: dict[str, str] = dataclasses.field(default_factory=dict)

    def has_labels(self, code: str) -> bool:
        return bool(self.wd_labels.get(code)) or bool(self.wd_aliases.get(code))


def _borrowed(code: str, regions: dict[str, dict], sources: Sources) -> str | None:
    """The code this one takes its labels from, if it has none of its own."""
    for donor in _donor_codes(code, regions, sources.succession, sources.has_labels):
        return donor
    return None


def _entry_for(code: str, base: dict, regions: dict[str, dict],
               sources: Sources) -> dict:
    """One gazetteer entry: the Eurostat names, plus whatever we can add."""
    labels = dict(sources.wd_labels.get(code, {}))
    aliases = set(sources.wd_aliases.get(code, set()))
    src = ["eurostat"] if not labels and not aliases else ["eurostat", "wikidata"]

    if not labels and not aliases:
        donor = _borrowed(code, regions, sources)
        if donor:
            labels = dict(sources.wd_labels.get(donor, {}))
            aliases = set(sources.wd_aliases.get(donor, set()))
            src = ["eurostat", f"wikidata:{donor}"]

    known = {fold(base["native"]), fold(base["latn"])}
    metro = sources.metro_labels.get(code)
    if metro and fold(metro) not in known:
        aliases.add(metro)
        src.append("euvoc")

    # The authority table is authoritative where it applies, so it wins over
    # Wikidata rather than merging with it; the Wikidata forms stay on as
    # aliases, which is where "Hellenic Republic" or "Holland" come from.
    official = sources.country_labels.get(code) if base["level"] == 0 else None
    if official:
        aliases |= {v for lang, v in labels.items() if v != official.get(lang)}
        labels = {**labels, **official}
        src.append("euvoc")

    kept = {lang: labels[lang] for lang in LANGUAGES if lang in labels}
    # An alias identical to a name the region already has carries nothing.
    taken = known | {fold(v) for v in kept.values()}
    return {
        "level": base["level"],
        "native": base["native"],
        "latn": base["latn"],
        "labels": kept,
        "aliases": sorted({a for a in aliases if a and fold(a) not in taken}),
        "src": src,
    }


def build_gazetteer(regions: dict[str, dict], sources: Sources) -> dict[str, dict]:
    """Merge the sources into one entry per region, recording provenance."""
    return {code: _entry_for(code, base, regions, sources)
            for code, base in sorted(regions.items())}


def coverage_report(gazetteer: dict[str, dict]) -> dict:
    """Per-language and per-level counts, printed after every build."""
    total = len(gazetteer)
    per_level: dict[int, list[int]] = {}
    for entry in gazetteer.values():
        bucket = per_level.setdefault(entry["level"], [0, 0])
        bucket[0] += 1
        bucket[1] += 1 if entry["labels"] else 0
    langs_each = [len(e["labels"]) for e in gazetteer.values()]
    return {
        "regions": total,
        "with_translations": sum(1 for n in langs_each if n),
        "average_languages": round(sum(langs_each) / total, 1) if total else 0,
        "per_language": {lang: sum(1 for e in gazetteer.values()
                                   if lang in e["labels"]) for lang in LANGUAGES},
        "per_level": {k: {"regions": v[0], "translated": v[1]}
                      for k, v in sorted(per_level.items())},
    }


def fetch_sources(regions: dict[str, dict]) -> Sources:
    """Pull the three label sources. Needs egress; logs what each gave."""
    country_labels = fetch_country_labels(
        [c for c, r in regions.items() if r["level"] == 0])
    logger.info("euvoc: %d countries", len(country_labels))
    succession = fetch_succession()
    logger.info("euvoc: %d succession entries", len(succession))
    metro_labels = fetch_metro_labels()
    logger.info("euvoc: %d metro labels", len(metro_labels))
    wd_labels, wd_aliases = fetch_wikidata_names()
    logger.info("wikidata: %d codes with labels, %d with aliases",
                len(wd_labels), len(wd_aliases))
    return Sources(country_labels=country_labels, wd_labels=wd_labels,
                   wd_aliases=wd_aliases, succession=succession,
                   metro_labels=metro_labels)


def write_document(path: pathlib.Path, gazetteer: dict[str, dict],
                   report: dict, version: str) -> None:
    """Write the artifact with one region per line — a generated file still
    gets read in diffs, and a single-line 1 MB JSON cannot be."""
    header = {
        "nuts_version": version,
        "generated": dt.date.today().isoformat(),
        "languages": list(LANGUAGES),
        "sources": {
            "eurostat": "GISCO NUTS_RG_10M (vendored, data/nuts/polygons)",
            "euvoc": "Publications Office country authority table + NUTS scheme",
            "wikidata": "P605 labels and aliases",
        },
        "coverage": report,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("{\n")
        for key, value in header.items():
            handle.write(f"  {json.dumps(key)}: "
                         f"{json.dumps(value, ensure_ascii=False)},\n")
        handle.write('  "regions": {\n')
        items = list(gazetteer.items())
        for index, (code, entry) in enumerate(items):
            tail = "" if index == len(items) - 1 else ","
            handle.write(f"    {json.dumps(code)}: "
                         f"{json.dumps(entry, ensure_ascii=False, sort_keys=True)}"
                         f"{tail}\n")
        handle.write("  }\n}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="2024", help="NUTS vintage to read")
    parser.add_argument("--out", type=pathlib.Path, default=OUTPUT_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="report coverage without writing the file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    regions = read_bundled_extras(read_eurostat_names(version=args.version))
    logger.info("eurostat: %d regions", len(regions))
    gazetteer = build_gazetteer(regions, fetch_sources(regions))

    report = coverage_report(gazetteer)
    logger.info("coverage: %s", json.dumps(report["per_level"]))
    logger.info("%d/%d regions translated, %.1f languages each",
                report["with_translations"], report["regions"],
                report["average_languages"])
    logger.info("thinnest languages: %s",
                sorted(report["per_language"].items(), key=lambda kv: kv[1])[:3])

    if args.dry_run:
        return 0
    write_document(args.out, gazetteer, report, args.version)
    logger.info("wrote %s (%.0f KB)", args.out, args.out.stat().st_size / 1024)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
