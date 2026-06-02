"""Sample-verify the wikidata mirror against live Wikidata.

Picks N random entities currently in our Virtuoso graph, refetches
truthy RDF from Wikidata, runs the same ``filter_graph`` we used when
writing, and diffs the expected triple set against what's actually in
Virtuoso. Reports per-entity counts plus an aggregate.

"Correct" means our stored set equals the filtered-fresh set. Drift
is expected (Wikidata mutates faster than we ingest), so the diff
report distinguishes:

  * missing   — triples in fresh Wikidata that are not in our graph
                (we are behind for this entity)
  * extra     — triples in our graph that fresh Wikidata no longer has
                (Wikidata removed them but we haven't been told yet)

Per-entity divergence at single-digit triples is normal background
churn. Divergence at "half the predicates" or "all triples missing"
is a bug.

Run inside the consumer pod so the chart's deps and env are right::

    kubectl -n fontem-prod exec deploy/fontem-wikidata-consumer -- \\
        python scripts/verify_wikidata_sample.py --n 24
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from collections import Counter

import httpx
from rdflib import Graph

# Allow `python scripts/verify_wikidata_sample.py` from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.relay.wikidata_fetcher import (  # noqa: E402  # pylint: disable=wrong-import-position
    fetch_truthy, FetchOutcome, make_client,
)
from src.relay.wikidata_writer import filter_graph, WIKIDATA_GRAPH  # noqa: E402  # pylint: disable=wrong-import-position

DEFAULT_SPARQL_URL = "http://virtuoso.fontem-prod.svc.cluster.local:8890/sparql"
ENTITY_PREFIX = "http://www.wikidata.org/entity/"


QID_MAX = 130_000_000  # rough current Wikidata Q-id ceiling 2026


def _ask(client: httpx.Client, sparql_url: str, entity_id: str) -> bool:
    """ASK whether the entity has any triples in our named graph."""
    iri = f"{ENTITY_PREFIX}{entity_id}"
    query = (f"ASK {{ GRAPH <{WIKIDATA_GRAPH}> "
             f"{{ <{iri}> ?p ?o }} }}")
    resp = client.post(
        sparql_url,
        data={"query": query, "format": "application/sparql-results+json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("boolean", False)


def pick_random_entities(client: httpx.Client, sparql_url: str,
                         n: int) -> list[str]:
    """Reject-sample random numeric Q-ids in [1, QID_MAX] against an
    ASK probe of our graph.

    Virtuoso's bif:rnd is evaluated once per query, not per row, so
    using it inside a FILTER would only filter or admit the entire
    result set — not draw a random sample. ASK probes are O(index)
    fast against our subject-keyed RDF_QUAD, so a few hundred probes
    cost less than a single SELECT-with-OFFSET scan."""
    picks: list[str] = []
    seen: set[str] = set()
    probes = 0
    # Cap probes so a tiny graph doesn't loop forever (our 4.7M-ish
    # entities in a 130M-id space → ~3% hit rate → ~30 probes per pick).
    while len(picks) < n and probes < n * 500:
        probes += 1
        qid = f"Q{random.randint(1, QID_MAX)}"
        if qid in seen:
            continue
        seen.add(qid)
        try:
            if _ask(client, sparql_url, qid):
                picks.append(qid)
        except httpx.HTTPError:
            continue
    return picks


def fetch_our_view(client: httpx.Client, sparql_url: str,
                   entity_id: str) -> Graph:
    """CONSTRUCT every triple in our graph whose subject is the
    entity. Mirror of the DELETE clause in the writer."""
    iri = f"{ENTITY_PREFIX}{entity_id}"
    query = f"""
    CONSTRUCT {{ <{iri}> ?p ?o }}
    WHERE {{ GRAPH <{WIKIDATA_GRAPH}> {{ <{iri}> ?p ?o }} }}
    """
    resp = client.post(
        sparql_url,
        data={"query": query},
        headers={"Accept": "text/turtle"},
        timeout=60,
    )
    resp.raise_for_status()
    g = Graph()
    if resp.text.strip():
        g.parse(data=resp.text, format="turtle")
    return g


def _triple_key(triple) -> tuple[str, str, str]:
    """Stable string key for set diff. rdflib Literal equality is
    sensitive to datatype-typed values vs lexical forms; using
    n-triples-ish strings keeps the comparison textual and
    predictable."""
    s, p, o = triple
    return (str(s), str(p), o.n3())


def compare(entity_id: str, ours: Graph, expected: Graph) -> dict:
    """Set-diff the two graphs by stable triple keys."""
    ours_keys = {_triple_key(t) for t in ours}
    exp_keys = {_triple_key(t) for t in expected}
    missing = exp_keys - ours_keys
    extra = ours_keys - exp_keys
    return {
        "entity": entity_id,
        "ours": len(ours_keys),
        "expected": len(exp_keys),
        "missing": len(missing),
        "extra": len(extra),
        "match": not missing and not extra,
        "sample_missing": list(missing)[:2],
        "sample_extra": list(extra)[:2],
    }


def _print_summary(results: list[dict],
                   fetch_outcomes: Counter[str]) -> None:
    """Aggregate stats + worst-drift sample so any pathological
    entity gets attention without having to scroll the per-row table."""
    matches = sum(1 for r in results if r["match"])
    avg_miss = sum(r["missing"] for r in results) / len(results)
    avg_xtra = sum(r["extra"] for r in results) / len(results)
    max_miss = max(r["missing"] for r in results)
    max_xtra = max(r["extra"] for r in results)
    print()
    print(f"compared:        {len(results)}")
    print(f"exact match:     {matches} ({100 * matches / len(results):.0f}%)")
    print(f"avg missing:     {avg_miss:.2f}  (max {max_miss})")
    print(f"avg extra:       {avg_xtra:.2f}  (max {max_xtra})")
    print(f"fetch outcomes:  {dict(fetch_outcomes)}")
    drifted = sorted(results, key=lambda r: r["missing"] + r["extra"],
                     reverse=True)
    if drifted and not drifted[0]["match"]:
        worst = drifted[0]
        print(f"\nworst drift: {worst['entity']} "
              f"missing={worst['missing']} extra={worst['extra']}")
        if worst["sample_missing"]:
            print(f"  e.g. missing: {worst['sample_missing'][0]}")
        if worst["sample_extra"]:
            print(f"  e.g. extra:   {worst['sample_extra'][0]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24,
                    help="how many entities to verify")
    ap.add_argument("--sparql-url", default=os.environ.get(
        "VIRTUOSO_SPARQL_URL", DEFAULT_SPARQL_URL))
    ap.add_argument("--entity", action="append", default=[],
                    help="verify this specific Q-id (repeatable); "
                         "if given, --n is ignored")
    args = ap.parse_args()

    # make_client carries the same User-Agent + connection-pool config
    # the relay/consumer use in prod. Wikidata returns 403 on bare
    # requests-default UA strings, so we must not use a plain Client().
    wiki_client = make_client()
    sparql_client = httpx.Client()  # local cluster, no UA needed
    if args.entity:
        picks = args.entity
        print(f"Verifying {len(picks)} specified entities")
    else:
        picks = pick_random_entities(sparql_client, args.sparql_url, args.n)
        print(f"Sampled {len(picks)} random entities")

    print()
    print(f"  {'entity':>12s}  {'ours':>5s} {'wiki':>5s}  "
          f"{'miss':>5s} {'xtra':>5s}  result")
    print(f"  {'-'*12}  {'-'*5} {'-'*5}  {'-'*5} {'-'*5}  -------")

    results: list[dict] = []
    fetch_outcomes: Counter[str] = Counter()
    for qid in picks:
        try:
            ours = fetch_our_view(sparql_client, args.sparql_url, qid)
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  {qid:>12s}  virtuoso read failed: {exc}")
            continue
        fr = fetch_truthy(qid, wiki_client)
        fetch_outcomes[fr.outcome.name] += 1
        if fr.outcome != FetchOutcome.OK or fr.graph is None:
            print(f"  {qid:>12s}  fetch outcome={fr.outcome.name} "
                  f"(ours={len(ours)})")
            continue
        expected = filter_graph(fr.graph, qid)
        r = compare(qid, ours, expected)
        results.append(r)
        verdict = "MATCH" if r["match"] else "DRIFT"
        print(f"  {qid:>12s}  {r['ours']:>5d} {r['expected']:>5d}  "
              f"{r['missing']:>5d} {r['extra']:>5d}  {verdict}")

    if not results:
        print("\nno comparable results")
        return 0
    _print_summary(results, fetch_outcomes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
