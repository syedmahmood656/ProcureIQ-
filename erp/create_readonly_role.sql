-- Phase 6 (Layer 4 of the SQL tool's safety stack): a database role that can ONLY read the
-- structured ERP tables. Even if the SQL validator AND the read-only-transaction guard both had
-- a bug, Postgres itself would still refuse any write -- and any read of tables outside this list
-- (contract_chunks, cuad_documents, system catalogs' sensitive parts) is denied too.
--
-- Optional but recommended. If you skip it, the SQL tool falls back to your main POSTGRES_USER and
-- still has the validator + READ ONLY transaction + timeout protecting it.
--
-- !! schema.sql DROPs and recreates every table, which also wipes these grants.
-- !! If you ever re-apply schema.sql, re-run this file afterwards.
--
-- Change the password below (and the matching POSTGRES_RO_PASSWORD in .env) for anything that
-- isn't a local dev box.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'procureiq_ro') THEN
        CREATE ROLE procureiq_ro LOGIN PASSWORD 'procureiq_ro';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE procureiq TO procureiq_ro;
GRANT USAGE ON SCHEMA public TO procureiq_ro;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM procureiq_ro;
GRANT SELECT ON vendors, contracts, purchase_orders, invoices, vendor_annual_spend TO procureiq_ro;

-- Belt and braces: every session by this role is read-only and time-limited by default.
ALTER ROLE procureiq_ro SET default_transaction_read_only = on;
ALTER ROLE procureiq_ro SET statement_timeout = '5s';
