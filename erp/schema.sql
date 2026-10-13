-- ProcureIQ schema — structured ERP-style data (Phase 1) + chunk-level full-text
-- search for hybrid retrieval (Phase 2 addition).
--
-- Phase 2 changes vs. the Phase 1 version of this file:
--   1. New `cuad_documents` table registers ALL 510 CUAD contracts (the chunker/embedder
--      run against the full corpus, not just the 120 vendor-linked ones from Phase 1).
--   2. `contracts` (the vendor-linked subset) now FKs to `cuad_documents` instead of
--      standing alone, so every ERP-linked contract is guaranteed to be a real,
--      indexed document.
--   3. New `contract_chunks` table stores the same chunks that go into Qdrant, with a
--      generated tsvector column + GIN index — this is the keyword half of hybrid search.

DROP TABLE IF EXISTS contract_chunks CASCADE;
DROP TABLE IF EXISTS invoices CASCADE;
DROP TABLE IF EXISTS purchase_orders CASCADE;
DROP TABLE IF EXISTS contracts CASCADE;
DROP TABLE IF EXISTS vendors CASCADE;
DROP TABLE IF EXISTS cuad_documents CASCADE;

CREATE TABLE vendors (
    vendor_id       SERIAL PRIMARY KEY,
    vendor_name     TEXT NOT NULL,
    category        TEXT NOT NULL,          -- e.g. 'Cloud Infrastructure', 'Logistics'
    country         TEXT NOT NULL,
    risk_tier       TEXT NOT NULL DEFAULT 'UNKNOWN',  -- populated later by risk-monitor tool
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

-- All 510 CUAD contracts, whether or not they're linked to a vendor in the ERP tables.
-- Mirrors data/raw/cuad/contracts_metadata.csv from Phase 1.
CREATE TABLE cuad_documents (
    contract_id         TEXT PRIMARY KEY,
    cuad_title          TEXT NOT NULL,
    source_file         TEXT NOT NULL,       -- relative path under data/raw/cuad/
    char_len            INTEGER NOT NULL,
    num_labeled_clauses INTEGER NOT NULL
);

CREATE TABLE contracts (
    contract_id     TEXT PRIMARY KEY REFERENCES cuad_documents(contract_id),
    vendor_id       INTEGER NOT NULL REFERENCES vendors(vendor_id),
    cuad_title      TEXT NOT NULL,
    source_file     TEXT NOT NULL,          -- relative path under data/raw/cuad/
    effective_date  DATE,
    expiration_date DATE,
    auto_renewal    BOOLEAN,
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE purchase_orders (
    po_id           SERIAL PRIMARY KEY,
    vendor_id       INTEGER NOT NULL REFERENCES vendors(vendor_id),
    po_date         DATE NOT NULL,
    amount          NUMERIC(14, 2) NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    status          TEXT NOT NULL DEFAULT 'OPEN'   -- OPEN, CLOSED, CANCELLED
);

CREATE TABLE invoices (
    invoice_id      SERIAL PRIMARY KEY,
    po_id           INTEGER NOT NULL REFERENCES purchase_orders(po_id),
    vendor_id       INTEGER NOT NULL REFERENCES vendors(vendor_id),
    invoice_date    DATE NOT NULL,
    amount          NUMERIC(14, 2) NOT NULL,
    paid_date       DATE,
    status          TEXT NOT NULL DEFAULT 'UNPAID'  -- UNPAID, PAID, OVERDUE
);

-- Phase 2: chunk-level storage for the keyword half of hybrid retrieval.
-- The dense-vector half of the same chunks lives in Qdrant (see ingestion/build_qdrant_index.py) —
-- chunk_id is the shared key that lets a result from either side be reconciled/deduped in Phase 3's
-- Reciprocal Rank Fusion step.
CREATE TABLE contract_chunks (
    chunk_id        TEXT PRIMARY KEY,
    contract_id     TEXT NOT NULL REFERENCES cuad_documents(contract_id),
    categories      TEXT[] NOT NULL,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    chunk_text      TEXT NOT NULL,
    search_vector   tsvector GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED
);

CREATE INDEX idx_po_vendor ON purchase_orders(vendor_id);
CREATE INDEX idx_invoice_vendor ON invoices(vendor_id);
CREATE INDEX idx_contracts_vendor ON contracts(vendor_id);
CREATE INDEX idx_chunk_contract ON contract_chunks(contract_id);
CREATE INDEX idx_chunk_categories ON contract_chunks USING GIN(categories);
CREATE INDEX idx_chunk_search ON contract_chunks USING GIN(search_vector);

-- Convenience view: exactly the kind of number the "Accountant" (SQL tool) will compute later.
CREATE OR REPLACE VIEW vendor_annual_spend AS
SELECT
    v.vendor_id,
    v.vendor_name,
    date_part('year', po.po_date) AS spend_year,
    SUM(po.amount) AS total_spend
FROM vendors v
JOIN purchase_orders po ON po.vendor_id = v.vendor_id
GROUP BY v.vendor_id, v.vendor_name, date_part('year', po.po_date);
