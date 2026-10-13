-- Phase 10: sanctions screening tables. IDEMPOTENT (safe to run repeatedly; never drops data),
-- so unlike erp/schema.sql it is safe to re-apply. ofac_loader.py runs this file for you.

CREATE TABLE IF NOT EXISTS sanctions_entries (
    entry_id    SERIAL PRIMARY KEY,
    ent_num     INTEGER NOT NULL,        -- OFAC's id for the listed party (aliases share it)
    name        TEXT NOT NULL,
    entry_type  TEXT NOT NULL,           -- entity | individual | vessel | aircraft
    program     TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL,           -- 'SDN' (primary name) | 'ALT' (alias)
    alt_type    TEXT NOT NULL DEFAULT '' -- aka | fka | nka for aliases
);
CREATE INDEX IF NOT EXISTS sanctions_entries_ent_idx ON sanctions_entries (ent_num);

-- One row: which OFAC files are loaded (provenance for every screening result).
CREATE TABLE IF NOT EXISTS sanctions_list_info (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    loaded_at   TIMESTAMP NOT NULL,
    sdn_sha256  TEXT NOT NULL,
    alt_sha256  TEXT NOT NULL,
    n_primary   INTEGER NOT NULL,
    n_alias     INTEGER NOT NULL
);

-- Latest screening result per vendor. Queryable by the SQL tool (allow-listed).
CREATE TABLE IF NOT EXISTS vendor_risk_screening (
    vendor_id        INTEGER PRIMARY KEY REFERENCES vendors(vendor_id) ON DELETE CASCADE,
    status           TEXT NOT NULL CHECK (status IN ('CLEAR', 'WEAK_MATCH', 'REVIEW')),
    best_match_name  TEXT,
    matched_on       TEXT,               -- primary | alias
    match_score      NUMERIC(4,3),       -- 0..1 NAME similarity, not a probability of guilt
    ofac_ent_num     INTEGER,
    ofac_entry_type  TEXT,
    ofac_program     TEXT,
    list_loaded_at   TIMESTAMP,
    screened_at      TIMESTAMP NOT NULL DEFAULT now()
);

-- The read-only SQL role (erp/create_readonly_role.sql) may read the screening results,
-- but NOT the 40k-row sanctions_entries table.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'procureiq_ro') THEN
        GRANT SELECT ON vendor_risk_screening TO procureiq_ro;
    END IF;
END
$$;
