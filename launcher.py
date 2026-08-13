import os
import re
import secrets

import psycopg
import uvicorn
from psycopg import sql
from starlette.responses import JSONResponse


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


MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("PORT", os.getenv("MCP_PORT", "8000")))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
MCP_ACCESS_TOKEN = os.getenv("MCP_ACCESS_TOKEN", "")

if not MCP_ACCESS_TOKEN:
    raise RuntimeError("Missing required environment variable: MCP_ACCESS_TOKEN")


def _normalize_path(path: str) -> str:
    path = "/" + (path or "").lstrip("/")
    return path.rstrip("/") or "/"


class BearerTokenMiddleware:
    def __init__(self, app, expected_token: str, protected_path: str):
        self.app = app
        self.expected_token = expected_token
        self.protected_path = _normalize_path(protected_path)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if _normalize_path(scope.get("path", "")) != self.protected_path:
            await self.app(scope, receive, send)
            return

        authorization = ""
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                authorization = value.decode("latin-1")
                break

        scheme, separator, token = authorization.partition(" ")
        valid = (
            bool(separator)
            and scheme.lower() == "bearer"
            and bool(token)
            and secrets.compare_digest(token, self.expected_token)
        )

        if not valid:
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


mcp_app = server.mcp.streamable_http_app(
    host=MCP_HOST,
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    json_response=True,
)

app = BearerTokenMiddleware(
    mcp_app,
    expected_token=MCP_ACCESS_TOKEN,
    protected_path=MCP_PATH,
)


if __name__ == "__main__":
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
