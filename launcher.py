import os
import re

import psycopg
from psycopg import sql


_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_schema = os.getenv("NEWS_SCHEMA", "news_agent").strip()
if not _SCHEMA_RE.fullmatch(_schema):
    raise RuntimeError(
        "NEWS_SCHEMA must be a simple PostgreSQL identifier: letters, digits, underscore; "
        "it cannot start with a digit"
    )

_original_connect = psycopg.connect


def _rewrite_sql(query, connection):
    if isinstance(query, str):
        text = query
    elif isinstance(query, sql.Composable):
        text = query.as_string(connection)
    else:
        return query

    quoted_schema = '"' + _schema.replace('"', '""') + '"'
    text = text.replace("news_agent.", f"{quoted_schema}.")
    text = text.replace("'news_agent'", "'" + _schema.replace("'", "''") + "'")
    return text


class _CursorProxy:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None, *, prepare=None, binary=None):
        query = _rewrite_sql(query, self._cursor.connection)
        return self._cursor.execute(query, params, prepare=prepare, binary=binary)

    def executemany(self, query, params_seq, *, returning=False):
        query = _rewrite_sql(query, self._cursor.connection)
        return self._cursor.executemany(query, params_seq, returning=returning)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._cursor.__exit__(exc_type, exc_val, exc_tb)

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _ConnectionProxy:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self, *args, **kwargs):
        return _CursorProxy(self._connection.cursor(*args, **kwargs))

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._connection.__exit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _connect(*args, **kwargs):
    return _ConnectionProxy(_original_connect(*args, **kwargs))


psycopg.connect = _connect

import server  # noqa: E402  # Import only after installing the schema rewrite layer.


if __name__ == "__main__":
    server.mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", os.getenv("MCP_PORT", "8000"))),
        streamable_http_path=os.getenv("MCP_PATH", "/mcp"),
        stateless_http=True,
        json_response=True,
    )
