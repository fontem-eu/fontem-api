// Phase 3 cutover — drop FinancialYear nodes from Neo4j.
//
// Run AFTER:
//  - both load_us_financials (EDGAR) and load_eu_listings.load_financials
//    (ESEF) have completed at least one full Virtuoso run, and the
//    /api/data-quality/edgar + /api/data-quality/esef endpoints
//    return Virtuoso-sourced numbers consistent with the Neo4j ones;
//  - the fontem-api Deployment has VIRTUOSO_SPARQL_URL set in prod (it
//    does, since Phase 2);
//  - the new fontem-api image with the SPARQL DQ source is deployed.
//
// Effect:
//  1. REPORTED edges between Company → FinancialYear are removed.
//  2. FinancialYear nodes are deleted.
//
// Note: unlike the sanctions cutover, FinancialYear had no
// metadata-bearing relationship that needs preserving — the year
// is repeated as an attribute on the node and in fontem:fiscalYear
// in Virtuoso. So no SanctionRef-style stub is needed.
//
// Reversal: re-run the Neo4j-era loaders before re-applying the
// new image (the old code path is preserved in git history at
// the commit prior to this PR's merge).

// 1) Drop REPORTED edges in 1k-row tx batches.
CALL {
    MATCH ()-[r:REPORTED]->(:FinancialYear)
    WITH r DELETE r
} IN TRANSACTIONS OF 1000 ROWS;

// 2) Drop FinancialYear nodes (already detached after step 1, but
// DETACH DELETE is the safe form).
CALL {
    MATCH (f:FinancialYear)
    WITH f DETACH DELETE f
} IN TRANSACTIONS OF 1000 ROWS;

// 3) Drop the legacy uniqueness constraint if it exists.
DROP CONSTRAINT financial_year_company_year IF EXISTS;
