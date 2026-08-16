"""
One-time secret bootstrap for the weather retrieval service.

Stores the Lakebase connection URL as `database/weather-lakebase-url`, creating
the scope if it doesn't exist. Idempotent - re-running it just overwrites the
value with whatever you supply.

    python setup_secrets.py

The value is resolved in this order, so the script works in a terminal, in a
notebook cell, and in CI:

    1. the LAKEBASE_URL environment variable  (no prompt at all)
    2. getpass, when stdin is a TTY           (masked)
    3. input(), otherwise                     (echoes - notebook cells only)

Prints key NAMES only, never values.

Note this app uses its own key rather than the shared `database/lakebase-url`
that the day-2 ticker-news app reads, so pointing one at a new database never
disturbs the other.

DO NOT run this with `%sh python setup_secrets.py` in a notebook - the subshell
has no TTY, so the prompt never reaches you and the cell hangs. In a notebook,
use a Python cell:  exec(open("setup_secrets.py").read())
"""

from __future__ import annotations

import os
import sys

SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
KEY = os.environ.get("LAKEBASE_SECRET_KEY", "weather-lakebase-url")


def _prompt(label: str, env_var: str) -> str:
    value = os.environ.get(env_var)
    if value:
        print(f"  {env_var} found in the environment; not prompting.")
        return value

    if sys.stdin.isatty():
        from getpass import getpass

        return getpass(f"  {label}: ").strip()

    print(f"  {label} (input will be visible - clear the cell output afterwards):")
    return input().strip()


def main() -> int:
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        print("The Databricks SDK is not installed. Run:\n"
              "    pip install -r requirements.txt")
        return 1

    try:
        client = WorkspaceClient()
        me = client.current_user.me().user_name
    except Exception as err:
        print(
            "Could not authenticate to Databricks.\n"
            f"  {type(err).__name__}: {err}\n\n"
            "Set up auth first, either:\n"
            "  databricks auth login --host https://<workspace>.cloud.databricks.com\n"
            "or:\n"
            "  export DATABRICKS_HOST=https://<workspace>.cloud.databricks.com\n"
            "  export DATABRICKS_TOKEN=<personal access token>\n"
        )
        return 1

    print(f"authenticated as {me}\n")

    url = _prompt("Lakebase connection URL", "LAKEBASE_URL")
    if not url:
        print("No URL supplied; nothing written.")
        return 1
    if not url.startswith("postgres"):
        print(f"That does not look like a Postgres URL (starts with {url[:12]!r}...).\n"
              "Expected: postgresql://<role>:<password>@<host>:5432/databricks_postgres?sslmode=require")
        return 1

    existing = {s.name for s in client.secrets.list_scopes()}
    if SCOPE not in existing:
        client.secrets.create_scope(scope=SCOPE)
        print(f"\ncreated secret scope {SCOPE!r}")
    else:
        print(f"\nsecret scope {SCOPE!r} already exists")

    client.secrets.put_secret(scope=SCOPE, key=KEY, string_value=url)
    print(f"stored secret {SCOPE}/{KEY}")

    try:
        from databricks.sdk.service.workspace import AclPermission

        client.secrets.put_acl(scope=SCOPE, principal="users", permission=AclPermission.READ)
        print(f"granted READ on {SCOPE!r} to 'users'")
    except Exception as err:
        # Non-fatal: on many workspaces the creator already has MANAGE and the
        # app's service principal is granted separately via the app resource.
        print(f"(could not set ACL, continuing: {err})")

    print("\nkeys now in scope (names only):")
    for secret in client.secrets.list_secrets(scope=SCOPE):
        print(f"  {SCOPE}/{secret.key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
