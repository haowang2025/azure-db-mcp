import os
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from mcp.server import MCPServer


mcp = MCPServer(
    "Azure PostgreSQL Read-Only",
    instructions=(
        "Read-only access to an Azure PostgreSQL database. "
        "Discover schemas and tables first, inspect table structure, then preview only a limited number of rows. "
        "This server intentionally does not expose arbitrary SQL or write operations."
    ),
)


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _connect():
    return psycopg.connect(
        host=_env("DBHOST"),
        port=int(os.getenv("DBPORT", "5432")),
        dbname=_env("DBNAME"),
        user=_env("DBUSER"),
        password=_env("DBPASSWORD"),
        sslmode=os.getenv("SSLMODE", "require"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
        options=(
            f"-c default_transaction_read_only=on "
            f"-c statement_timeout={int(os.getenv('DB_STATEMENT_TIMEOUT_MS', '15000'))}"
        ),
        row_factory=dict_row,
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _safe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: _json_safe(value) for key, value in row.items()}
        for row in rows
    ]


def _table_exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = %s
                  AND table_type IN ('BASE TABLE', 'VIEW')
            ) AS exists
            """,
            (schema, table),
        )
        row = cur.fetchone()
        return bool(row and row["exists"])


@mcp.tool()
def db_status() -> dict[str, Any]:
    """Check database connectivity and report the current database/user without changing data."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    current_database() AS database,
                    current_user AS db_user,
                    current_schema() AS current_schema,
                    current_setting('transaction_read_only') AS transaction_read_only,
                    current_setting('statement_timeout') AS statement_timeout,
                    now() AS server_time
                """
            )
            row = cur.fetchone()
            return {key: _json_safe(value) for key, value in row.items()}


@mcp.tool()
def list_schemas() -> list[str]:
    """List non-system schemas that the MCP database user can inspect."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT schema_name
                FROM information_schema.schemata
                WHERE schema_name NOT LIKE 'pg_%'
                  AND schema_name <> 'information_schema'
                ORDER BY schema_name
                """
            )
            return [row["schema_name"] for row in cur.fetchall()]


@mcp.tool()
def list_tables(schema: str = "public") -> list[dict[str, str]]:
    """
    List readable tables and views in a schema.

    Args:
        schema: PostgreSQL schema name, usually "public".
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type IN ('BASE TABLE', 'VIEW')
                ORDER BY table_name
                """,
                (schema,),
            )
            return [
                {"table": row["table_name"], "type": row["table_type"]}
                for row in cur.fetchall()
            ]


@mcp.tool()
def describe_table(table: str, schema: str = "public") -> dict[str, Any]:
    """
    Describe columns for a known table or view.

    Args:
        table: Table or view name returned by list_tables.
        schema: PostgreSQL schema name.
    """
    with _connect() as conn:
        if not _table_exists(conn, schema, table):
            raise ValueError("Table or view does not exist or is not accessible.")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    column_name,
                    data_type,
                    is_nullable,
                    column_default,
                    ordinal_position
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema, table),
            )
            columns = _safe_rows(cur.fetchall())

        return {
            "schema": schema,
            "table": table,
            "columns": columns,
        }


@mcp.tool()
def preview_table(
    table: str,
    limit: int = 20,
    schema: str = "public",
) -> dict[str, Any]:
    """
    Preview a small number of rows from a known table or view.

    This tool is read-only and does not accept arbitrary SQL.

    Args:
        table: Table or view name returned by list_tables.
        limit: Number of rows to return, clamped to 1..100.
        schema: PostgreSQL schema name.
    """
    limit = max(1, min(int(limit), 100))

    with _connect() as conn:
        if not _table_exists(conn, schema, table):
            raise ValueError("Table or view does not exist or is not accessible.")

        with conn.cursor() as cur:
            query = sql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
                sql.Identifier(schema),
                sql.Identifier(table),
            )
            cur.execute(query, (limit,))
            rows = _safe_rows(cur.fetchall())

        return {
            "schema": schema,
            "table": table,
            "limit": limit,
            "row_count": len(rows),
            "rows": rows,
        }


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", os.getenv("MCP_PORT", "8000"))),
        streamable_http_path=os.getenv("MCP_PATH", "/mcp"),
        stateless_http=True,
        json_response=True,
    )
