-- Read-only SQL role for a MANAGED/PUBLIC Postgres (Neon etc.). Unlike create_readonly_role.sql this
-- takes the password as a parameter, so no well-known password is ever created:
--
--   psql "<connection url>" -v ro_password='<long random password>' -f erp/create_readonly_role_cloud.sql
--
-- Safe to re-run: it rotates the password and re-applies the grants (needed after every restore).
-- (psql variables are not expanded inside DO $$ blocks, hence format() + \gexec.)

SELECT format('CREATE ROLE procureiq_ro LOGIN PASSWORD %L', :'ro_password')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'procureiq_ro') \gexec

SELECT format('ALTER ROLE procureiq_ro PASSWORD %L', :'ro_password') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO procureiq_ro', current_database()) \gexec

GRANT USAGE ON SCHEMA public TO procureiq_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM procureiq_ro;
GRANT SELECT ON vendors, contracts, purchase_orders, invoices, vendor_annual_spend TO procureiq_ro;

-- Phase 10 screening results (only if that table exists in this database)
SELECT 'GRANT SELECT ON vendor_risk_screening TO procureiq_ro'
 WHERE to_regclass('public.vendor_risk_screening') IS NOT NULL \gexec

ALTER ROLE procureiq_ro SET default_transaction_read_only = on;
ALTER ROLE procureiq_ro SET statement_timeout = '5s';
