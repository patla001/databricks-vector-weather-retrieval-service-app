"""
Lakebase (Databricks-managed Postgres) connection helper.

Connects using a single LAKEBASE_URL (a standard Postgres connection URL,
e.g. postgresql://role:password@host:5432/databricks_postgres?sslmode=require)
pointing at a native Postgres role with a static, non-expiring password.
This keeps setup to a single secret instead of five separate env vars.

The URL comes from LAKEBASE_URL when set, and otherwise from the Databricks
secret scope. Both the deployed app and local development use the env var: on
Databricks Apps a secret *resource* injects it (see the `valueFrom` entry in
app.yaml), locally it comes from .env. The secret-scope branch is a fallback for
a deployment whose resource hasn't been configured.

Two drivers are supported, selected by WEATHER_DB_DRIVER:

    psycopg2  the default, and what the Flask app uses
    pg8000    pure Python, and the only one that works in a Databricks
              serverless job task

The second exists because psycopg2-binary bundles its own libssl/libcrypto,
while a serverless kernel has already loaded OpenSSL through grpc and pyarrow.
Two OpenSSL builds in one process abort on the first TLS handshake, so the task
dies with "Fatal error: The Python kernel is unresponsive" - on *connect*, not
on import, which is why an import-only probe looks fine. pg8000 has no C
extension and does TLS through Python's own `ssl` module, so there is only ever
one OpenSSL in play.

Both drivers are wrapped to the same surface: `.cursor()` yields dict rows and
accepts `%s` and `%(name)s` placeholders, so callers never branch on the driver.
"""

import base64
import logging
import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Sequence

import re
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


def _pick_driver() -> str:
    """psycopg2 unless asked otherwise, falling back to whatever is installed."""
    choice = os.environ.get("WEATHER_DB_DRIVER", "").strip().lower()
    if choice in ("psycopg2", "pg8000"):
        return choice
    try:
        import psycopg2  # noqa: F401
        return "psycopg2"
    except ImportError:
        return "pg8000"


DRIVER = _pick_driver()


@lru_cache(maxsize=1)
def _client() -> WorkspaceClient:
    """Build the Databricks client on first use.

    Constructed lazily so that importing this module doesn't require Databricks
    auth - a local run with LAKEBASE_URL set never needs a workspace at all.
    """
    return WorkspaceClient()


@lru_cache(maxsize=1)
def _lakebase_url() -> str:
    """Return the Lakebase connection URL from the env var or the secret scope.

    Cached because get_connection() calls this on every single connection, and
    the secret-scope branch is a network round trip to the Databricks API - one
    per database call. The URL points at a static, non-expiring password role, so
    it can't go stale mid-process.

    Logs which source won (never the value). A secret resource that resolves to
    an empty string falls through to the scope silently otherwise, and the two
    are indistinguishable from the outside.
    """
    url = os.environ.get("LAKEBASE_URL")
    if url:
        logger.info("Lakebase URL resolved from the LAKEBASE_URL environment variable")
        return url
    logger.info("Lakebase URL resolved from secret scope %s/%s", _SCOPE, _KEY)
    secret = _client().secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


# ---------------------------------------------------------------------------
# pg8000 adapters
#
# pg8000 returns plain tuples and its own cursor object. Everything downstream
# was written against psycopg2's RealDictCursor, so rather than teach every
# caller about drivers, the pg8000 cursor is wrapped to the same shape.
# ---------------------------------------------------------------------------


class _Pg8000Cursor:
    """A pg8000 cursor that returns dicts, like psycopg2's RealDictCursor."""

    def __init__(self, cursor):
        self._cursor = cursor

    def _columns(self) -> list[str]:
        description = self._cursor.description or []
        return [
            c[0].decode() if isinstance(c[0], (bytes, bytearray)) else c[0]
            for c in description
        ]

    def execute(self, sql, params=None):
        # pg8000 rejects an explicit None where psycopg2 accepts it, and a
        # statement containing a literal % breaks if it is handed params at all.
        if params is None:
            return self._cursor.execute(sql)
        return self._cursor.execute(sql, params)

    def fetchall(self) -> list[dict]:
        columns = self._columns()
        return [dict(zip(columns, row)) for row in self._cursor.fetchall()]

    def fetchone(self) -> dict | None:
        row = self._cursor.fetchone()
        return None if row is None else dict(zip(self._columns(), row))

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def close(self):
        self._cursor.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Pg8000Connection:
    """A pg8000 connection whose .cursor() yields dict rows."""

    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return _Pg8000Cursor(self._connection.cursor())

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def _connect_pg8000(url: str):
    import ssl
    from urllib.parse import parse_qs, unquote, urlparse

    import pg8000.dbapi

    # pg8000 defaults to 'format' (%s only). The read queries in this project
    # use named placeholders, and 'pyformat' accepts both styles.
    pg8000.dbapi.paramstyle = "pyformat"

    parsed = urlparse(url)
    sslmode = (parse_qs(parsed.query).get("sslmode") or ["require"])[0]

    # Lakebase terminates TLS with a certificate this container has no root for,
    # and `sslmode=require` in Postgres means "encrypt", not "verify" - psycopg2
    # behaves the same way for this URL. Verification would need sslmode=verify-full
    # plus a CA bundle.
    context = ssl.create_default_context()
    if sslmode in ("require", "prefer", "allow"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    return _Pg8000Connection(
        pg8000.dbapi.connect(
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            host=parsed.hostname,
            port=parsed.port or 5432,
            database=(parsed.path or "/").lstrip("/") or "databricks_postgres",
            ssl_context=context if sslmode != "disable" else None,
        )
    )


def _connect_psycopg2(url: str):
    import psycopg2
    from psycopg2.extras import RealDictCursor

    return psycopg2.connect(url, cursor_factory=RealDictCursor)


@contextmanager
def get_connection():
    """Yield a connection whose cursors return dict rows, on either driver."""
    url = _lakebase_url()
    conn = _connect_pg8000(url) if DRIVER == "pg8000" else _connect_psycopg2(url)
    try:
        yield conn
    finally:
        conn.close()


def get_engine():
    """Return a SQLAlchemy engine for Lakebase."""
    from sqlalchemy import create_engine

    url = _lakebase_url()
    if DRIVER == "pg8000":
        url = re.sub(r"^postgres(ql)?://", "postgresql+pg8000://", url, count=1)
    return create_engine(url)


def execute_values(cur, sql: str, rows: Sequence[Sequence], template: str,
                   page_size: int = 200, fetch: bool = False) -> list[dict]:
    """Multi-row INSERT, batched, on either driver.

    Replaces psycopg2.extras.execute_values, which does not exist for pg8000.
    The signature and semantics are kept so call sites read the same, including
    `fetch`: batching means a plain cur.fetchall()/rowcount afterwards describes
    only the final batch and undercounts every earlier one.

    `template` is the per-row placeholder group, e.g. "(%s,%s,%s::jsonb)" - the
    casts matter, since neither driver infers ::vector or ::jsonb from a string.
    """
    if not rows:
        return []

    collected: list[dict] = []
    for start in range(0, len(rows), page_size):
        page = rows[start:start + page_size]
        values_clause = ",".join([template] * len(page))
        statement = re.sub(r"VALUES\s+%s", "VALUES " + values_clause, sql, count=1)
        flat = tuple(value for row in page for value in row)
        cur.execute(statement, flat)
        if fetch:
            collected.extend(cur.fetchall())
    return collected


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query against Lakebase and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE against Lakebase, return affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount
