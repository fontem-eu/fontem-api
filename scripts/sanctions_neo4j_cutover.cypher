// Phase 2 cutover — drop SanctionedEntity body data from Neo4j.
//
// Run AFTER:
//  - the dual-write loader has shipped to prod and at least one
//    daily run has populated <http://data.fontem.eu/graph/sanctions>
//    in Virtuoso (verified via /data-quality/sanctions returning
//    Virtuoso-sourced numbers);
//  - the read paths in graph_data_quality.py have flipped to
//    Virtuoso for entity counts, regimes, completeness;
//  - the fontem-api Deployment has VIRTUOSO_SPARQL_URL set in prod.
//
// Effect:
//  1. Each SANCTIONED edge gets re-pointed at a :SanctionRef stub
//     that holds only the Virtuoso IRI; the edge metadata
//     (confidence, tier, since, reviewed, etc.) stays put.
//  2. SanctionedEntity bodies are deleted.
//  3. Constraint on the entity_id key is dropped (no nodes to
//     constrain anymore).
//
// Reversal: re-run the legacy loader with VIRTUOSO_SPARQL_ENDPOINT
// unset and the Neo4j writes will repopulate the entity bodies.
// SanctionRef stubs become orphaned but harmless.

// Step 1 — for each existing SANCTIONED edge, mint a SanctionRef
// pointing at the Virtuoso IRI built from the entity_id.
MATCH (c:Company)-[r:SANCTIONED]->(s:SanctionedEntity)
WITH c, r, s,
     'http://data.fontem.eu/id/Sanction/' + s.entity_id AS iri
MERGE (ref:SanctionRef {iri: iri})
WITH c, r, s, ref
CREATE (c)-[r2:SANCTIONED]->(ref)
SET r2 = properties(r);

// Step 2 — drop the original SANCTIONED edges (now duplicated
// onto the stubs) and the SanctionedEntity nodes.
CALL {
    MATCH ()-[r:SANCTIONED]->(:SanctionedEntity)
    WITH r DELETE r
} IN TRANSACTIONS OF 1000 ROWS;

CALL {
    MATCH (s:SanctionedEntity)
    WITH s DETACH DELETE s
} IN TRANSACTIONS OF 1000 ROWS;

// Step 3 — drop the legacy uniqueness constraint.
DROP CONSTRAINT sanctioned_entity_id IF EXISTS;
