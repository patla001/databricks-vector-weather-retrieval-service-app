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
"""

import base64
import logging
import os
from contextlib import contextmanager
from functools import lru_cache

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import RealDictCursor
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


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


@contextmanager
def get_connection():
    """Yield a raw psycopg2 connection with a RealDictCursor factory."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def get_engine():
    """Return a SQLAlchemy engine for Lakebase."""
    return create_engine(_lakebase_url())


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
