import json
import os
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from mcp.server import MCPServer

DEFAULT_SCHEMA = os.getenv("NEWS_SCHEMA", "news_agent")
ALLOWED_SCHEMAS = {
    value.strip()
    for value in os.getenv("ALLOWED_SCHEMAS", "news_agent").split(",")
    if value.strip()
}
STATEMENT_TIMEOUT_MS = max(
    1000, min(int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "30000")), 120000)
)
ACTIVE_JOB_STATUSES = (
    "QUEUED",
    "DISPATCHING",
    "AGENT_B_RUNNING",
    "WAITING_TOOL",
    "WAITING_SUBAGENT",
    "REPORT_GENERATING",
    "REPORT_WRITING",
    "RETRYING",
)
WRITABLE_JOB_STATUSES = {
    "AGENT_B_RUNNING",
    "WAITING_TOOL",
    "WAITING_SUBAGENT",
    "REPORT_GENERATING",
    "REPORT_WRITING",
    "RETRYING",
}
TERMINAL_JOB_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}

mcp = MCPServer(
    "News Intelligence PostgreSQL - Agent B",
    instructions=(
        "Controlled read/write access to the News Intelligence PostgreSQL database for Agent B. "
        "The news_agent schema is the source of truth for PID news inputs, RID report versions, "
        "JOB_ID analysis jobs, subjects, relations, and evidence. Prefer domain tools over generic "
        "table inspection. Agent B may update only its own workflow state/progress/heartbeat, finalize "
        "its requested report, record failures, and append evidence to existing relations. There is no "
        "arbitrary SQL tool, no DDL tool, and no general DELETE or table-mutation tool."
    ),
)


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_connection():
    return psycopg.connect(
        host=_env("DBHOST"),
        port=int(os.getenv("DBPORT", "5432")),
        dbname=_env("DBNAME"),
        user=_env("DBUSER"),
        password=_env("DBPASSWORD"),
        sslmode=os.getenv("SSLMODE", "require"),
        connect_timeout=max(1, min(int(os.getenv("DB_CONNECT_TIMEOUT", "10")), 60)),
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
        row_factory=dict_row,
    )


def json_safe(value: Any):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def _row(row):
    return None if row is None else {k: json_safe(v) for k, v in row.items()}


def _rows(rows):
    return [_row(row) for row in rows]


def _limit(value: int, maximum: int = 100) -> int:
    return max(1, min(int(value), maximum))


def _clip(value: str | None, max_chars: int) -> str | None:
    if value is None:
        return None
    max_chars = max(100, min(int(max_chars), 300000))
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n\n[TRUNCATED original_chars={len(value)}]"


def _schema(schema: str) -> str:
    schema = (schema or DEFAULT_SCHEMA).strip()
    if schema not in ALLOWED_SCHEMAS:
        raise ValueError(f"Schema {schema!r} is not allowed: {sorted(ALLOWED_SCHEMAS)}")
    return schema


def _table_exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema=%s AND table_name=%s
                  AND table_type IN ('BASE TABLE','VIEW')
            ) AS ok
            """,
            (schema, table),
        )
        row = cur.fetchone()
        return bool(row and row["ok"])


def _fetch_job(conn, job_id: str, for_update: bool = False) -> dict:
    suffix = sql.SQL(" FOR UPDATE OF j, r") if for_update else sql.SQL("")
    query = sql.SQL(
        """
        SELECT j.*, r.report_version, r.status AS report_status, r.prompt_version,
               n.title, n.canonical_url, n.primary_subject_id
        FROM news_agent.news_analysis_jobs j
        JOIN news_agent.news_reports r ON r.rid=j.requested_rid
        JOIN news_agent.news_inputs n ON n.pid=j.pid
        WHERE j.job_id=%s
        """
    ) + suffix
    with conn.cursor() as cur:
        cur.execute(query, (job_id,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"Unknown JOB_ID: {job_id}")
    if row["job_type"] != "AGENT_B_ANALYSIS":
        raise ValueError(f"JOB_ID {job_id} is not an AGENT_B_ANALYSIS job")
    return dict(row)


def _fetch_report(conn, rid: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM news_agent.news_reports WHERE rid=%s", (rid,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"Unknown RID: {rid}")
    return dict(row)


@mcp.tool()
def db_status() -> dict:
    """Check connectivity and current database/session settings."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_database() AS database, current_user AS db_user,
                   now() AS server_time,
                   current_setting('transaction_read_only') AS transaction_read_only,
                   current_setting('statement_timeout') AS statement_timeout,
                   to_regnamespace('news_agent') IS NOT NULL AS news_agent_schema_exists
            """
        )
        return _row(cur.fetchone())


@mcp.tool()
def system_overview() -> dict:
    """Return compact counts and workflow status distribution."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT count(*) FROM news_agent.news_inputs) AS news_count,
              (SELECT count(*) FROM news_agent.news_reports) AS report_count,
              (SELECT count(*) FROM news_agent.news_analysis_jobs) AS job_count,
              (SELECT count(*) FROM news_agent.news_subjects) AS subject_count,
              (SELECT count(*) FROM news_agent.subject_relations) AS relation_count,
              (SELECT count(*) FROM news_agent.relation_evidence) AS evidence_count,
              (SELECT count(*) FROM news_agent.knowledge_projection_outbox WHERE status='PENDING') AS projection_pending_count
            """
        )
        counts = _row(cur.fetchone())
        cur.execute("SELECT status,count(*) AS count FROM news_agent.news_analysis_jobs GROUP BY status ORDER BY status")
        jobs = _rows(cur.fetchall())
        cur.execute("SELECT status,count(*) AS count FROM news_agent.news_reports GROUP BY status ORDER BY status")
        reports = _rows(cur.fetchall())
    return {"counts": counts, "job_statuses": jobs, "report_statuses": reports}


@mcp.tool()
def list_tables(schema: str = DEFAULT_SCHEMA) -> list[str]:
    """List accessible tables/views in an allowed schema."""
    schema = _schema(schema)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=%s AND table_type IN ('BASE TABLE','VIEW')
            ORDER BY table_name
            """,
            (schema,),
        )
        return [row["table_name"] for row in cur.fetchall()]


@mcp.tool()
def describe_table(table: str, schema: str = DEFAULT_SCHEMA) -> dict:
    """Describe columns and indexes for a known table/view."""
    schema = _schema(schema)
    with get_connection() as conn:
        if not _table_exists(conn, schema, table):
            raise ValueError("Table/view does not exist or is not accessible")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name,data_type,udt_name,is_nullable,column_default,ordinal_position
                FROM information_schema.columns WHERE table_schema=%s AND table_name=%s
                ORDER BY ordinal_position
                """,
                (schema, table),
            )
            columns = _rows(cur.fetchall())
            cur.execute(
                "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s ORDER BY indexname",
                (schema, table),
            )
            indexes = _rows(cur.fetchall())
    return {"schema": schema, "table": table, "columns": columns, "indexes": indexes}


@mcp.tool()
def preview_table(table: str, limit: int = 20, schema: str = DEFAULT_SCHEMA) -> list[dict]:
    """Preview up to 100 rows from a known table/view; no arbitrary SQL is accepted."""
    schema = _schema(schema)
    limit = _limit(limit)
    with get_connection() as conn:
        if not _table_exists(conn, schema, table):
            raise ValueError("Table/view does not exist or is not accessible")
        with conn.cursor() as cur:
            query = sql.SQL("SELECT * FROM {}.{} LIMIT %s").format(sql.Identifier(schema), sql.Identifier(table))
            cur.execute(query, (limit,))
            return _rows(cur.fetchall())


@mcp.tool()
def get_news(pid: str, include_content: bool = True, max_content_chars: int = 80000) -> dict:
    """Get one PID plus primary subject and newest job state."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT n.*, s.canonical_name,s.subject_type,s.wind_id,s.wind_code,s.wind_name,
                   s.wind_exchange,s.wind_match_status,
                   j.job_id AS latest_job_id,j.requested_rid AS latest_job_rid,
                   j.status AS latest_job_status,j.current_stage AS latest_job_stage,
                   j.progress AS latest_job_progress,j.agent_b_task_id,
                   j.error_code,j.error_message,j.updated_at AS latest_job_updated_at
            FROM news_agent.news_inputs n
            LEFT JOIN news_agent.news_subjects s ON s.subject_id=n.primary_subject_id
            LEFT JOIN LATERAL (
              SELECT * FROM news_agent.news_analysis_jobs j2 WHERE j2.pid=n.pid
              ORDER BY j2.created_at DESC LIMIT 1
            ) j ON true
            WHERE n.pid=%s
            """,
            (pid,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"Unknown PID: {pid}")
    row = dict(row)
    if include_content:
        row["content_text"] = _clip(row.get("content_text"), max_content_chars)
    else:
        row.pop("content_text", None)
    return _row(row)


@mcp.tool()
def search_news(query: str, limit: int = 20) -> list[dict]:
    """Search historical news and include the latest completed report version when available."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = _limit(limit, 50)
    p = f"%{query}%"
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT n.pid,n.title,n.publisher,n.published_at,n.status,n.canonical_url,
                   left(n.content_text,800) AS content_preview,
                   s.subject_id,s.canonical_name,s.subject_type,s.wind_code,
                   r.rid AS latest_completed_rid,r.report_version AS latest_completed_version,
                   r.completed_at AS latest_completed_at,left(coalesce(r.report_markdown,''),1200) AS report_preview
            FROM news_agent.news_inputs n
            LEFT JOIN news_agent.news_subjects s ON s.subject_id=n.primary_subject_id
            LEFT JOIN LATERAL (
              SELECT * FROM news_agent.news_reports rr
              WHERE rr.pid=n.pid AND rr.status='COMPLETED'
              ORDER BY rr.report_version DESC LIMIT 1
            ) r ON true
            WHERE n.title ILIKE %s OR n.content_text ILIKE %s OR n.canonical_url ILIKE %s OR s.canonical_name ILIKE %s
            ORDER BY coalesce(n.published_at,n.created_at) DESC LIMIT %s
            """,
            (p, p, p, p, limit),
        )
        return _rows(cur.fetchall())


@mcp.tool()
def list_report_versions(pid: str, limit: int = 20) -> list[dict]:
    """List RID versions for a PID without full Markdown."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT rid,pid,subject_id,report_version,status,prompt_version,agent_b_task_id,
                   report_path,version_report_path,created_at,started_at,completed_at,updated_at
            FROM news_agent.news_reports WHERE pid=%s ORDER BY report_version DESC LIMIT %s
            """,
            (pid, _limit(limit)),
        )
        return _rows(cur.fetchall())


@mcp.tool()
def get_latest_report(pid: str, completed_only: bool = True, max_markdown_chars: int = 120000) -> dict:
    """Get highest-version COMPLETED report by default, matching Deepnote App display semantics."""
    with get_connection() as conn, conn.cursor() as cur:
        if completed_only:
            cur.execute(
                "SELECT * FROM news_agent.news_reports WHERE pid=%s AND status='COMPLETED' ORDER BY report_version DESC LIMIT 1",
                (pid,),
            )
        else:
            cur.execute("SELECT * FROM news_agent.news_reports WHERE pid=%s ORDER BY report_version DESC LIMIT 1", (pid,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"No {'completed ' if completed_only else ''}report found for PID: {pid}")
    row = dict(row)
    row["report_markdown"] = _clip(row.get("report_markdown"), max_markdown_chars)
    return _row(row)


@mcp.tool()
def get_report(rid: str, max_markdown_chars: int = 120000) -> dict:
    """Get one exact RID including Agent A/B metadata and Markdown."""
    with get_connection() as conn:
        row = _fetch_report(conn, rid)
    row["report_markdown"] = _clip(row.get("report_markdown"), max_markdown_chars)
    return _row(row)


@mcp.tool()
def get_job(job_id: str) -> dict:
    """Get exact JOB_ID state; pure read."""
    with get_connection() as conn:
        return _row(_fetch_job(conn, job_id))


@mcp.tool()
def get_jobs_for_pid(pid: str, limit: int = 20) -> list[dict]:
    """List workflow jobs for a PID, newest first."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM news_agent.news_analysis_jobs WHERE pid=%s ORDER BY created_at DESC LIMIT %s",
            (pid, _limit(limit)),
        )
        return _rows(cur.fetchall())


@mcp.tool()
def list_active_jobs(limit: int = 20) -> list[dict]:
    """List active Agent B jobs without mutating them."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.*,n.title,s.canonical_name,s.wind_code
            FROM news_agent.news_analysis_jobs j
            JOIN news_agent.news_inputs n ON n.pid=j.pid
            LEFT JOIN news_agent.news_subjects s ON s.subject_id=n.primary_subject_id
            WHERE j.job_type='AGENT_B_ANALYSIS' AND j.status=ANY(%s)
            ORDER BY j.updated_at ASC LIMIT %s
            """,
            (list(ACTIVE_JOB_STATUSES), _limit(limit)),
        )
        return _rows(cur.fetchall())


@mcp.tool()
def get_agent_b_context(job_id: str, max_content_chars: int = 180000) -> dict:
    """Return the full research context for one Agent B JOB_ID."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.job_id,j.pid,j.requested_rid AS rid,j.status AS job_status,j.current_stage,j.progress,
                   j.agent_b_task_id,j.attempt_count,j.max_attempts,j.request_payload,j.response_metadata,
                   n.title,n.content_text,n.canonical_url,n.publisher,n.published_at,n.primary_subject_id AS subject_id,
                   s.canonical_name,s.subject_type,s.wind_id,s.wind_code,s.wind_name,s.wind_exchange,s.wind_match_status,
                   r.agent_a_result,r.report_version,r.prompt_version,r.status AS report_status
            FROM news_agent.news_analysis_jobs j
            JOIN news_agent.news_inputs n ON n.pid=j.pid
            JOIN news_agent.news_reports r ON r.rid=j.requested_rid
            LEFT JOIN news_agent.news_subjects s ON s.subject_id=n.primary_subject_id
            WHERE j.job_id=%s
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Unknown JOB_ID: {job_id}")
        row = dict(row)
        relations = []
        if row.get("subject_id"):
            cur.execute(
                """
                SELECT rel.*,fs.canonical_name AS from_name,fs.wind_code AS from_wind_code,
                       ts.canonical_name AS to_name,ts.wind_code AS to_wind_code
                FROM news_agent.subject_relations rel
                JOIN news_agent.news_subjects fs ON fs.subject_id=rel.from_subject_id
                JOIN news_agent.news_subjects ts ON ts.subject_id=rel.to_subject_id
                WHERE rel.from_subject_id=%s OR rel.to_subject_id=%s
                ORDER BY rel.updated_at DESC LIMIT 200
                """,
                (row["subject_id"], row["subject_id"]),
            )
            relations = cur.fetchall()
    return json_safe({
        "job_id": row["job_id"], "pid": row["pid"], "rid": row["rid"], "subject_id": row.get("subject_id"),
        "job": {k: row.get(k) for k in ["job_status","current_stage","progress","agent_b_task_id","attempt_count","max_attempts","request_payload","response_metadata"]},
        "subject": {"name": row.get("canonical_name"),"type": row.get("subject_type"),"wind_id": row.get("wind_id"),"wind_code": row.get("wind_code"),"wind_name": row.get("wind_name"),"wind_exchange": row.get("wind_exchange"),"wind_match_status": row.get("wind_match_status")},
        "source": {"title": row.get("title"),"url": row.get("canonical_url"),"publisher": row.get("publisher"),"published_at": row.get("published_at"),"content_text": _clip(row.get("content_text"), max_content_chars)},
        "agent_a_result": row.get("agent_a_result") or {},
        "known_relations": relations,
        "report": {"report_version": row.get("report_version"),"prompt_version": row.get("prompt_version"),"status": row.get("report_status")},
    })


@mcp.tool()
def get_news_mentions(pid: str) -> list[dict]:
    """Return subjects mentioned in one PID with roles/evidence."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.*,s.canonical_name,s.subject_type,s.wind_code
            FROM news_agent.subject_mentions m JOIN news_agent.news_subjects s ON s.subject_id=m.subject_id
            WHERE m.pid=%s ORDER BY CASE WHEN m.mention_role='primary_subject' THEN 0 ELSE 1 END,m.created_at
            """,
            (pid,),
        )
        return _rows(cur.fetchall())


@mcp.tool()
def search_subjects(query: str, limit: int = 20) -> list[dict]:
    """Search subjects by name, aliases, Wind ID/code/name."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    p = f"%{query}%"
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM news_agent.news_subjects
            WHERE canonical_name ILIKE %s OR aliases::text ILIKE %s OR wind_id ILIKE %s OR wind_code ILIKE %s OR wind_name ILIKE %s
            ORDER BY CASE WHEN lower(canonical_name)=lower(%s) THEN 0 ELSE 1 END,updated_at DESC LIMIT %s
            """,
            (p, p, p, p, p, query, _limit(limit, 50)),
        )
        return _rows(cur.fetchall())


@mcp.tool()
def get_subject_context(subject_id: str, news_limit: int = 30, relation_limit: int = 100) -> dict:
    """Return subject, identifiers, relations and related historical news."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM news_agent.news_subjects WHERE subject_id=%s", (subject_id,))
        subject = cur.fetchone()
        if subject is None:
            raise ValueError(f"Unknown subject_id: {subject_id}")
        cur.execute("SELECT * FROM news_agent.subject_identifiers WHERE subject_id=%s ORDER BY is_primary DESC,identifier_type", (subject_id,))
        identifiers = cur.fetchall()
        cur.execute(
            """
            SELECT r.*,fs.canonical_name AS from_name,fs.wind_code AS from_wind_code,
                   ts.canonical_name AS to_name,ts.wind_code AS to_wind_code
            FROM news_agent.subject_relations r
            JOIN news_agent.news_subjects fs ON fs.subject_id=r.from_subject_id
            JOIN news_agent.news_subjects ts ON ts.subject_id=r.to_subject_id
            WHERE r.from_subject_id=%s OR r.to_subject_id=%s ORDER BY r.updated_at DESC LIMIT %s
            """,
            (subject_id, subject_id, _limit(relation_limit, 200)),
        )
        relations = cur.fetchall()
        cur.execute(
            """
            SELECT n.pid,n.title,n.publisher,n.published_at,n.status,n.canonical_url,
                   r.rid AS latest_completed_rid,r.report_version AS latest_completed_version,r.completed_at AS latest_completed_at
            FROM news_agent.news_inputs n
            LEFT JOIN LATERAL (
              SELECT * FROM news_agent.news_reports rr WHERE rr.pid=n.pid AND rr.status='COMPLETED'
              ORDER BY rr.report_version DESC LIMIT 1
            ) r ON true
            WHERE n.primary_subject_id=%s OR EXISTS(
              SELECT 1 FROM news_agent.subject_mentions m WHERE m.pid=n.pid AND m.subject_id=%s
            )
            ORDER BY coalesce(n.published_at,n.created_at) DESC LIMIT %s
            """,
            (subject_id, subject_id, _limit(news_limit)),
        )
        news = cur.fetchall()
    return {"subject": _row(subject), "identifiers": _rows(identifiers), "relations": _rows(relations), "news": _rows(news)}


@mcp.tool()
def search_relations(query: str, limit: int = 30) -> list[dict]:
    """Search known relations by type, subject name, or Wind code."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    p = f"%{query}%"
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.*,fs.canonical_name AS from_name,fs.wind_code AS from_wind_code,
                   ts.canonical_name AS to_name,ts.wind_code AS to_wind_code,
                   (SELECT count(*) FROM news_agent.relation_evidence e WHERE e.relation_id=r.relation_id) AS evidence_count
            FROM news_agent.subject_relations r
            JOIN news_agent.news_subjects fs ON fs.subject_id=r.from_subject_id
            JOIN news_agent.news_subjects ts ON ts.subject_id=r.to_subject_id
            WHERE r.relation_type ILIKE %s OR fs.canonical_name ILIKE %s OR ts.canonical_name ILIKE %s OR fs.wind_code ILIKE %s OR ts.wind_code ILIKE %s
            ORDER BY r.updated_at DESC LIMIT %s
            """,
            (p, p, p, p, p, _limit(limit)),
        )
        return _rows(cur.fetchall())


@mcp.tool()
def get_relation_evidence(relation_id: str, limit: int = 50) -> list[dict]:
    """Return source-traceable evidence for a relation."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.*,n.title,n.publisher,n.published_at,n.canonical_url
            FROM news_agent.relation_evidence e JOIN news_agent.news_inputs n ON n.pid=e.pid
            WHERE e.relation_id=%s ORDER BY e.created_at DESC LIMIT %s
            """,
            (relation_id, _limit(limit)),
        )
        return _rows(cur.fetchall())


@mcp.tool()
def list_relation_types() -> list[dict]:
    """List relation catalog semantics."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM news_agent.relation_type_catalog ORDER BY relation_type")
        return _rows(cur.fetchall())


@mcp.tool()
def mark_job_running(job_id: str, task_id: str | None = None, stage: str = "RUNNING", progress: float = 0.05, metadata: dict[str, Any] | None = None) -> dict:
    """Mark an accepted Agent B job running and set its report RUNNING."""
    progress = max(0.0, min(float(progress), 0.99))
    with get_connection() as conn:
        with conn.transaction():
            job = _fetch_job(conn, job_id, for_update=True)
            if job["status"] in TERMINAL_JOB_STATUSES:
                raise ValueError(f"Cannot start terminal job {job_id}: {job['status']}")
            with conn.cursor() as cur:
                cur.execute("SELECT news_agent.set_job_status(%s,'AGENT_B_RUNNING',%s,%s,%s,%s::jsonb)", (job_id, stage, progress, task_id, json.dumps(metadata or {}, ensure_ascii=False)))
                cur.execute("UPDATE news_agent.news_reports SET status=CASE WHEN status IN ('PENDING','RETRYING') THEN 'RUNNING' ELSE status END,started_at=coalesce(started_at,now()),agent_b_task_id=coalesce(%s,agent_b_task_id),updated_at=now() WHERE rid=%s", (task_id, job["requested_rid"]))
        return _row(_fetch_job(conn, job_id))


@mcp.tool()
def heartbeat_agent_b_job(job_id: str, stage: str | None = None, progress: float | None = None, task_id: str | None = None, metadata: dict[str, Any] | None = None) -> dict:
    """Refresh heartbeat/progress for an active Agent B job."""
    with get_connection() as conn:
        with conn.transaction():
            job = _fetch_job(conn, job_id, for_update=True)
            if job["status"] not in WRITABLE_JOB_STATUSES:
                raise ValueError(f"Job is not in a writable active state: {job['status']}")
            p = job["progress"] if progress is None else max(0.0, min(float(progress), 0.99))
            with conn.cursor() as cur:
                cur.execute("SELECT news_agent.set_job_status(%s,%s,%s,%s,%s,%s::jsonb)", (job_id, job["status"], stage, p, task_id, json.dumps(metadata or {}, ensure_ascii=False)))
        return _row(_fetch_job(conn, job_id))


@mcp.tool()
def update_job_progress(job_id: str, status: str = "AGENT_B_RUNNING", stage: str | None = None, progress: float | None = None, task_id: str | None = None, metadata: dict[str, Any] | None = None) -> dict:
    """Move an active Agent B job among allowed non-terminal workflow states."""
    status = (status or "").upper()
    if status not in WRITABLE_JOB_STATUSES:
        raise ValueError(f"Unsupported status {status!r}; allowed={sorted(WRITABLE_JOB_STATUSES)}")
    with get_connection() as conn:
        with conn.transaction():
            job = _fetch_job(conn, job_id, for_update=True)
            if job["status"] in TERMINAL_JOB_STATUSES:
                raise ValueError(f"Cannot update terminal job {job_id}: {job['status']}")
            p = job["progress"] if progress is None else max(0.0, min(float(progress), 0.99))
            with conn.cursor() as cur:
                cur.execute("SELECT news_agent.set_job_status(%s,%s,%s,%s,%s,%s::jsonb)", (job_id, status, stage, p, task_id, json.dumps(metadata or {}, ensure_ascii=False)))
                cur.execute("UPDATE news_agent.news_reports SET status=CASE WHEN status IN ('PENDING','RETRYING') THEN 'RUNNING' ELSE status END,started_at=coalesce(started_at,now()),agent_b_task_id=coalesce(%s,agent_b_task_id),updated_at=now() WHERE rid=%s", (task_id, job["requested_rid"]))
        return _row(_fetch_job(conn, job_id))


@mcp.tool()
def complete_agent_b_job(job_id: str, report_markdown: str, agent_b_result: dict[str, Any] | None = None, tool_summary: dict[str, Any] | None = None, evidence_summary: dict[str, Any] | None = None, task_id: str | None = None) -> dict:
    """Finalize the JOB_ID's requested RID and mark report/job/news COMPLETED."""
    report_markdown = (report_markdown or "").strip()
    if not report_markdown:
        raise ValueError("report_markdown is required")
    with get_connection() as conn:
        with conn.transaction():
            job = _fetch_job(conn, job_id, for_update=True)
            if job["status"] == "COMPLETED":
                report = _fetch_report(conn, job["requested_rid"])
                return {"already_completed": True, "job": _row(job), "report": _row(report)}
            if job["status"] in {"FAILED", "CANCELLED"}:
                raise ValueError(f"Cannot complete terminal job {job_id}: {job['status']}")
            with conn.cursor() as cur:
                if task_id:
                    cur.execute("SELECT news_agent.set_job_status(%s,'REPORT_WRITING','WRITING_REPORT',0.98,%s,'{}'::jsonb)", (job_id, task_id))
                cur.execute(
                    "SELECT news_agent.finalize_report(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)",
                    (job_id, job["requested_rid"], report_markdown, None, None,
                     json.dumps(agent_b_result or {}, ensure_ascii=False),
                     json.dumps(tool_summary or {}, ensure_ascii=False),
                     json.dumps(evidence_summary or {}, ensure_ascii=False)),
                )
        final_job = _fetch_job(conn, job_id)
        final_report = _fetch_report(conn, job["requested_rid"])
    return {"already_completed": False, "job": _row(final_job), "report": _row(final_report)}


@mcp.tool()
def fail_agent_b_job(job_id: str, error_code: str, error_message: str, retryable: bool = False, metadata: dict[str, Any] | None = None) -> dict:
    """Mark an Agent B job FAILED or RETRYING via the existing database retry policy."""
    error_message = (error_message or "").strip()
    if not error_message:
        raise ValueError("error_message is required")
    with get_connection() as conn:
        with conn.transaction():
            job = _fetch_job(conn, job_id, for_update=True)
            if job["status"] == "COMPLETED":
                raise ValueError(f"Cannot fail completed job {job_id}")
            with conn.cursor() as cur:
                if metadata:
                    cur.execute("SELECT news_agent.set_job_status(%s,%s,%s,%s,%s,%s::jsonb)", (job_id, job["status"], job["current_stage"], job["progress"], job["agent_b_task_id"], json.dumps(metadata, ensure_ascii=False)))
                cur.execute("SELECT news_agent.fail_job(%s,%s,%s,%s)", (job_id, (error_code or "AGENT_B_FAILED")[:200], error_message[:4000], bool(retryable)))
        return _row(_fetch_job(conn, job_id))


@mcp.tool()
def add_relation_evidence(relation_id: str, pid: str, evidence_text: str, rid: str | None = None, is_supporting: bool = True, confidence: float | None = None, evidence_location: dict[str, Any] | None = None, extraction_method: str = "AgentB", extractor_version: str = "agent-b-v1") -> dict:
    """Append/update Agent B evidence for an EXISTING relation; does not create relations."""
    evidence_text = (evidence_text or "").strip()
    if not evidence_text:
        raise ValueError("evidence_text is required")
    if confidence is not None:
        confidence = max(0.0, min(float(confidence), 1.0))
    with get_connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT 1 FROM news_agent.subject_relations WHERE relation_id=%s", (relation_id,))
            if cur.fetchone() is None:
                raise ValueError(f"Unknown relation_id: {relation_id}")
            cur.execute("SELECT 1 FROM news_agent.news_inputs WHERE pid=%s", (pid,))
            if cur.fetchone() is None:
                raise ValueError(f"Unknown PID: {pid}")
            if rid is not None:
                cur.execute("SELECT 1 FROM news_agent.news_reports WHERE rid=%s AND pid=%s", (rid, pid))
                if cur.fetchone() is None:
                    raise ValueError(f"RID {rid} does not belong to PID {pid}")
            cur.execute(
                """
                INSERT INTO news_agent.relation_evidence(
                  relation_id,pid,rid,evidence_text,evidence_location,is_supporting,confidence,extraction_method,extractor_version
                ) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
                ON CONFLICT(relation_id,pid,evidence_text) DO UPDATE SET
                  rid=coalesce(EXCLUDED.rid,news_agent.relation_evidence.rid),
                  evidence_location=news_agent.relation_evidence.evidence_location||EXCLUDED.evidence_location,
                  is_supporting=EXCLUDED.is_supporting,
                  confidence=coalesce(EXCLUDED.confidence,news_agent.relation_evidence.confidence),
                  extraction_method=EXCLUDED.extraction_method,extractor_version=EXCLUDED.extractor_version
                RETURNING *
                """,
                (relation_id, pid, rid, evidence_text, json.dumps(evidence_location or {}, ensure_ascii=False), bool(is_supporting), confidence, extraction_method, extractor_version),
            )
            return _row(cur.fetchone())


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", os.getenv("MCP_PORT", "8000"))),
        streamable_http_path=os.getenv("MCP_PATH", "/mcp"),
        stateless_http=True,
        json_response=True,
    )
