-- Run this as the database owner/admin with psql.
-- Supply the password as a psql variable, for example:
--   psql "host=... dbname=... user=... sslmode=require" \
--     -v mcp_password='A_LONG_RANDOM_PASSWORD' \
--     -f db/setup_readonly.sql
--
-- This script grants read-only access to the public schema and configures
-- defensive read-only/session limits for the MCP database role.

\if :{?mcp_password}
\else
\echo 'ERROR: provide -v mcp_password=...'
\quit
\endif

DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chatgpt_mcp') THEN
        CREATE ROLE chatgpt_mcp LOGIN;
    END IF;
END
$do$;

ALTER ROLE chatgpt_mcp PASSWORD :'mcp_password';
ALTER ROLE chatgpt_mcp SET default_transaction_read_only = on;
ALTER ROLE chatgpt_mcp SET statement_timeout = '15s';

SELECT format('GRANT CONNECT ON DATABASE %I TO chatgpt_mcp', current_database()) \gexec

GRANT USAGE ON SCHEMA public TO chatgpt_mcp;
REVOKE CREATE ON SCHEMA public FROM chatgpt_mcp;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO chatgpt_mcp;

-- Applies to future tables created by the role executing this script.
-- If another owner creates tables, run the equivalent ALTER DEFAULT PRIVILEGES
-- as that owner too.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT ON TABLES TO chatgpt_mcp;

\echo 'Read-only role chatgpt_mcp is configured for the current database.'
