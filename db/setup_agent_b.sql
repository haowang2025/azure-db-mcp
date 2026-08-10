-- Configure a least-privilege PostgreSQL role for the Agent B MCP.
-- Run as the database owner/admin with psql, for example:
--
--   psql "host=... port=5432 dbname=... user=... sslmode=require" \
--     -v mcp_password='A_LONG_RANDOM_PASSWORD' \
--     -f db/setup_agent_b.sql
--
-- The role can read the news_agent knowledge/workflow schema and perform only
-- the writes needed by Agent B: workflow state/progress, report finalization,
-- failures/retries, and evidence on existing relations. No DELETE/DDL/general
-- arbitrary mutation privilege is granted.

\if :{?mcp_password}
\else
\echo 'ERROR: provide -v mcp_password=...'
\quit
\endif

DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'news_agent_b_mcp') THEN
        CREATE ROLE news_agent_b_mcp LOGIN;
    END IF;
END
$do$;

ALTER ROLE news_agent_b_mcp PASSWORD :'mcp_password';
ALTER ROLE news_agent_b_mcp RESET default_transaction_read_only;
ALTER ROLE news_agent_b_mcp SET statement_timeout = '30s';

SELECT format('GRANT CONNECT ON DATABASE %I TO news_agent_b_mcp', current_database()) \gexec
GRANT USAGE ON SCHEMA news_agent TO news_agent_b_mcp;

-- Agent B research context.
GRANT SELECT ON ALL TABLES IN SCHEMA news_agent TO news_agent_b_mcp;

-- Existing workflow functions are SECURITY INVOKER, so the role also needs
-- column-level UPDATE privileges used inside those functions.
GRANT UPDATE (
    status,
    current_stage,
    progress,
    agent_b_task_id,
    attempt_count,
    error_code,
    error_message,
    response_metadata,
    started_at,
    heartbeat_at,
    finished_at,
    updated_at
) ON news_agent.news_analysis_jobs TO news_agent_b_mcp;

GRANT UPDATE (
    status,
    report_markdown,
    report_path,
    version_report_path,
    agent_b_result,
    tool_summary,
    evidence_summary,
    agent_b_task_id,
    started_at,
    completed_at,
    updated_at
) ON news_agent.news_reports TO news_agent_b_mcp;

GRANT UPDATE (
    status,
    updated_at
) ON news_agent.news_inputs TO news_agent_b_mcp;

-- Agent B may append/refresh evidence only for an existing relation.
GRANT INSERT (
    relation_id,
    pid,
    rid,
    evidence_text,
    evidence_location,
    is_supporting,
    confidence,
    extraction_method,
    extractor_version
) ON news_agent.relation_evidence TO news_agent_b_mcp;

GRANT UPDATE (
    rid,
    evidence_location,
    is_supporting,
    confidence,
    extraction_method,
    extractor_version
) ON news_agent.relation_evidence TO news_agent_b_mcp;

GRANT EXECUTE ON FUNCTION news_agent.new_id(text) TO news_agent_b_mcp;
GRANT EXECUTE ON FUNCTION news_agent.set_job_status(text, text, text, numeric, text, jsonb) TO news_agent_b_mcp;
GRANT EXECUTE ON FUNCTION news_agent.finalize_report(text, text, text, text, text, jsonb, jsonb, jsonb) TO news_agent_b_mcp;
GRANT EXECUTE ON FUNCTION news_agent.fail_job(text, text, text, boolean) TO news_agent_b_mcp;

-- Future tables remain readable for research, but future write privileges are
-- intentionally NOT granted automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA news_agent
GRANT SELECT ON TABLES TO news_agent_b_mcp;

\echo 'Agent B MCP role news_agent_b_mcp configured for news_agent.'
