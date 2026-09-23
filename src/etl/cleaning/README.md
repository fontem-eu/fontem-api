# Ingest cleaning stage

The cleaning stage of `gitops/docs/roadmap/data-backlog.md` Part 5 (C2–C5): a
**pure rule library** that runs on every TED notice after the parser and after
identity assignment, before the event payloads are built. It never touches a
database or a service; the loader (`src/etl/load_ted_contracts.py`) builds
`NoticeFacts` from the parsed notice, injects `Lookups` (peer stats, the
country map) and applies the `CleaningResult`.

Flow: `eforms-parser → identity (matcher) → clean → event store`, with one
documented exception — **identifier normalisation (C3) runs before the
matcher**, because identity comes from the matcher and the matcher needs the
identifier. It is deterministic, so a re-run never re-keys an entity.
`gmr_id` generation is unchanged: it still hashes the raw published name or
VAT.

Principles (owner decisions): never rewrite silently — the raw value stays on
the event, every rule that fired is listed in `cleaning_rules`, and an
ambiguous value is quarantined (`value_quarantine_reason`) rather than guessed.
Country- and gateway-specific rules are wanted.

## Rule table

| Rule id | Family | What it does | Outcome | Fixture |
|---|---|---|---|---|
| `it.notice_text_in_supplier_name` | C2 name | Word-bounded, case-insensitive match of the Italian award-decree boilerplate: `gara aggiudicata`, `come da determina`, `determina n. <digits>`, `pubblicat(a\|o) sul sito`, `si veda`, `aggiudicat(a\|o) con`, `lotto n. <digits>`. The plan's bare `vedi ` is **not** here: measured on prod it matches 116 real companies (TRIVEDI, MEDVEDI, CHATURVEDI, Arvedi, the Hungarian UGYVEDI IRODA family), and even word-bounded `vedi` is a real name (VEDI TOURGROUP s.r.o.) | supplier withheld | `tests/fixtures/ted/184512-2013.xml` — contractor `OFFICIALNAME` "Gara aggiudicata come da determina n. 543 del 2013 pubblicata sul sito www.csc.sanita.fvg.it alla sezione delibere e decreti." |
| `generic.name_contains_url` | C2 name | A URL with a scheme (`http://`, `https://`) anywhere in the name; or a bare `www.` domain when the name reads as a phrase (>= 4 tokens) and carries no legal form. A plain domain name is **not** enough: WWW.KOTVA.CZ a.s., www.heymountain.com GmbH, WWW.THEARTISANCO.CO.UK LIMITED and HTTPS OU are real companies trading under their domain | supplier withheld | same notice (`... pubblicata sul sito www.csc.sanita.fvg.it ...`), and the Catalan gateway's `DIFERENTES ADJUDICATARIOS (https://contractaciopublica.cat/...)` |
| `es.multiple_awardees_placeholder` | C2 name | The ES/CA gateways' "several winners, see the resolution": `varios\|diversos\|diferentes\|distintos adjudicatari*`, `diversos homologad*\|homologat*` | supplier withheld | `Varios adjudicatarios (ver resolución adjudicación)`, `DIVERSOS HOMOLOGATS - https://contractaciopublica.cat/...` (prod, 2026-09-22) |
| `generic.name_is_placeholder` | C2 name | After trim + casefold the name is one of `n/a`, `na`, `não aplicável`, `nao aplicavel`, `vários`, `varios`, `diversi`, `-`, `.`, `1`, `0`, `x`, `xxx` | supplier withheld | the placeholder family from the 2026-09-21 audit (unit strings) |
| `generic.name_is_sentence` | C2 name | > 80 chars AND >= 8 whitespace tokens AND (a `n. <digits> del <year>` clause OR a function-word ratio >= 0.35 from small stopword lists for it/pt/es/fr/de/en/nl/pl/ro, the list chosen by `notice_language`, all lists when unknown). A legal-form token (S.p.A., Lda, GmbH, Sp. z o.o., Ltd, ... — `lexicon.py`) exempts a name from THIS rule only | supplier withheld | same 2013 notice (decree clause) plus unit strings |
| `generic.national_id_country_prefixed` | C3 identifier | When the legal id has no scheme (or VAT/NATIONAL/EORI) and `canon_vat(value)` is None, try `canon_vat(<VAT prefix of the org's country> + value)`; the per-country regex in `canon_vat` is the guard. Alpha-2 or alpha-3 accepted; Greece → `EL`. Suppliers AND buyers | canonical VAT handed to the matcher; counted when it changed the outcome | `tests/fixtures/ted/646890-2026.xml` — Beja, supplier NIF `503536717` + `PRT` → `PT503536717`; DE Leitweg `053660036036-31001-86` + `DEU` stays None |
| `generic.value_scale_sibling_ratio` | C4 scale | `scale_normalization` tier A unchanged: award total ~x1000 its own estimate with the cents fingerprint or a proven gateway | rescale /1000, `value_scale_corrected = "ratio"` | Figueira da Foz school award (existing `tests/test_load_ted_contracts.py::test_milli_euro_leak_rescaled_at_emit`) |
| `pt.value_scale_country_prior` | C4 scale | `scale_normalization` tier B unchanged: all fields consistent but >= EUR 1B on a PRT notice | rescale /1000, `value_scale_corrected = "country_prior"` | existing `tests/test_scale_normalization.py` cases |
| `generic.value_peer_outlier` | C4 scale | With injected peer stats for (buyer country, cpv4) and n >= 30: if the chosen value > K × p90 (K = 50; K = 20 when a gateway placeholder is present — `tender_result_award_date_raw` starts `2000-01-01` or `tender_reference == "0.0"`) then QUARANTINE with reason `ambiguous_scale_x100_or_x1000` when value/100 or value/1000 lands in [p10, p99], else `implausible_vs_peers`. Never rescales. Inactive (logged once) when `dq.peer_value_stats` is absent | value withheld exactly like the scorer's quarantines; candidates in the review-queue note | Beja 646890-2026 — EUR 24,474,133 integer, CPV 32420000, PRT: /100 lands on the PT 3242* median |
| `generic.date_placeholder` | C5 dates | `2000-01-01` / `1900-01-01` in `award_date_raw`, `tender_result_award_date_raw`, `publication_date`, `issue_date`. Behaviour unchanged — `_as_day` keeps dropping them from the typed fields; the rule counts them | counted; raw kept on the event | the PT gateway watermark (unit strings) |

Outcomes for a notice: `withheld` (org id → rule id), `identifiers` (org id →
canonical VAT or None, for every organisation), `value` (`Keep` | `Rescale` |
`Quarantine`), `rules_fired` (ordered, unique), `counters` (rule id → hits) and
the raw `outcomes` the dry-run report samples.

A supplier hit by several name rules is withheld under the FIRST in
`rules/__init__.py::NAME_RULES` (most specific first: Italian boilerplate,
ES/CA multiple-awardees, URL, placeholder, sentence); every hit is still
counted.

**Why the false-positive guards matter.** Withholding deletes a supplier
from its award: no company node, no `AWARDED_TO` edge, and nothing later
can recover the name. So every pattern here was measured against the real
graph before it shipped, and the ones that hit real companies were
narrowed rather than kept (see the `vedi` and `www.` notes above). Value rules run in sequence — the peer test sees the value after the
milli-euro tiers.

## What the loader emits

On `UpsertContract`: `cleaning_rules`, `suppliers_withheld` (each `{name_raw,
reason, role, org_id}`; absent from `parties[]` and never `company_gmr_id`, so
the neo4j sink cannot stub a company for it), `value_raw`, `award_date_raw`,
`tender_result_award_date_raw`, `tender_reference`, `notice_language`,
`eforms_sdk`, `value_quarantine_reason`, and — whenever the notice published
them — `framework_max_value_eur`, `framework_reestimated_value_eur`,
`framework_duration_months`, `framework_max_operators`.

`framework_id` (with `framework_id_source`) is the eForms **OPT-100 Framework
Notice Identifier**, which eforms-parser 0.13 reads from
`efac:NoticeResult/efac:SettledContract/cac:NoticeDocumentReference/cbc:ID`
(fallback BT-125 `cac:TenderingProcess/cac:NoticeDocumentReference/cbc:ID` on a
framework procedure) and normalises — the zero-padded publication number
`00536632-2024` becomes `536632-2024`, the form TED's own `framework-notice-id`
index matches, and the eForms UUID loses its `-NN` version suffix. It rides on
**every** notice that publishes one, not only call-offs: the
framework-establishing award notice and each call-off under it carry the
IDENTICAL value, and that symmetry is the whole mechanism by which they find
each other. Coverage on framework-flagged award notices: 28.8% (2024), 89.4%
(2025), 98.3% (2026).

It is a grouping KEY, not a pointer to a contract we hold. ~80% of the time it
names a call for competition, and this loader ingests award and modification
notices only, so the referenced notice resolves to a `:Contract` in the graph
about 13.6% of the time. Nothing may read an establishment-then-call-off order
out of it either — both ends carry the same value, and `is_framework` (the
lot's `ContractingSystemTypeCode` starting `fa`) is on 344 of 351 sampled
call-offs too.

`UpsertFrameworkAgreement` is keyed on that same OPT-100 value, so the node and
the contracts pointing at it share one key space (it used to be keyed on
`contract_key`, i.e. BT-04 `ContractFolderID` — a different value space, which
no `framework_id` could ever name). A notice with framework terms but no
OPT-100 key emits no agreement rather than inventing one. Emission still
happens only when `EMIT_FRAMEWORK_AGREEMENTS=true` (the sink that understands
the type must be deployed first).

The buyer's normalised identifier is computed and counted but not emitted:
`match_authority` does not read it and `UpsertAuthority` has no VAT field.

## Dry run

`python -m src.etl.load_ted_contracts --dry-run [--report PATH]` runs the
pipeline with an in-memory event log (payloads validated, nothing stored), no
watermark, raw-store or review-queue writes, and re-processes notices already
in the graph. The report is JSON: `totals`, `by_rule`, `by_country`, `by_year`,
`examples` (first 20 per rule).

## Extension point: the classifier (layer 3)

Not implemented. When it comes: add a rule class in `rules/names.py` that reads
`facts.suppliers` and returns `SupplierWithheld` outcomes, append it to
`NAME_RULES` after the four hard-signal rules, and feed it precomputed scores
through `Lookups` — a rule never calls a service. It may only withhold, never
rename or merge.
