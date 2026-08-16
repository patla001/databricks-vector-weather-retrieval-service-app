# Deploying to Databricks Apps

Runbook for `dbc-7e085092-52e4.cloud.databricks.com`. Every command below is
copy-pasteable; nothing here has been run against the account yet.

**Nothing in this repo provisions cloud resources on import or on deploy.** Step 1
is the only step that starts billing, and it is a single explicit command.

---

## Current state of the workspace

Checked with the CLI (`databricks` v1.11.0, `DEFAULT` profile), read-only:

| | |
|---|---|
| Workspace | `https://dbc-7e085092-52e4.cloud.databricks.com` |
| Identity | Ezer Patlan &lt;epatlan1742@sdsu.edu&gt; |
| Lakebase instances | **none** — `databricks database list-database-instances` returns `[]` |
| Existing apps | `lakebase-support-app`, `massive-lakebase-app` (both ACTIVE) |
| Secret scopes | `database`, `massive`, `support` |
| Keys in `database` | `lakebase-url` (points at the deleted `massive-sync-db`) |

> ⚠️ **Both existing apps are pointed at a database that no longer exists.**
> `massive-sync-db.database.cloud.databricks.com` does not resolve. That is why
> this app uses its own secret key (`database/weather-lakebase-url`) — so
> creating a database for the weather app never silently repoints the day-2 app.

---

## Step 1 — Create a Lakebase instance (⚠️ starts billing)

This is the only step that costs money, and it bills until the instance is
stopped or deleted. Valid capacities are `CU_1`, `CU_2`, `CU_4`, `CU_8`.

```bash
export DATABRICKS_CONFIG_PROFILE=DEFAULT

databricks database create-database-instance weather-vector-db \
  --capacity CU_1 \
  --enable-pg-native-login
```

`--enable-pg-native-login` is required: `lakebase.py` authenticates with a single
static-password DSN, and without native login the instance issues only
short-lived OAuth tokens, which that pattern cannot use.

Wait for `AVAILABLE` (the command blocks up to 20 minutes by default), then:

```bash
databricks database get-database-instance weather-vector-db
```

**Stop it when you're not using it** — this is the lever that controls cost:

```bash
databricks database update-database-instance weather-vector-db --stopped
# ... and to remove it entirely:
databricks database delete-database-instance weather-vector-db
```

### Create the password role

In the workspace UI: **Catalog → Lakebase → `weather-vector-db` → Roles & Databases**

1. Confirm native/password authentication is enabled.
2. **Add role** → authentication method **Password** → name it `weather_app`.
3. Copy the connection URL it shows:

```
postgresql://weather_app:<password>@<host>:5432/databricks_postgres?sslmode=require
```

> ⚠️ **Paste the host whole.** The host Databricks shows already includes its own
> domain. Pasting it into a template that already ends in
> `.database.cloud.databricks.com` produces a doubled hostname whose psycopg2
> error reads `password authentication failed` — which sends you rotating a
> password that was never wrong. Verify with `nslookup <host>` first.

---

## Step 2 — Store the DSN as a secret

```bash
python setup_secrets.py       # prompts, masked; writes database/weather-lakebase-url
```

Or non-interactively:

```bash
LAKEBASE_URL='postgresql://weather_app:...@...:5432/databricks_postgres?sslmode=require' \
  python setup_secrets.py
```

Verify (prints names, never values):

```bash
databricks secrets list-secrets database
```

---

## Step 3 — Create the schema

Run in the **Lakebase instance's own query editor**, opened from the database
instance page.

> ⚠️ Not a workspace SQL editor and not a `%sql` cell — those target Unity
> Catalog, cannot see these Postgres tables, and fail on `GRANT ... TO weather_app`
> with `PRINCIPAL_DOES_NOT_EXIST` because they read the role as a Databricks
> principal rather than a Postgres role.

Paste in order:

1. `sql/00_grant_app_role.sql` — only if `weather_app` is not in `databricks_superuser`
2. `sql/01_setup_weather_documents.sql`
3. `sql/02_setup_weather_embeddings.sql` — `CREATE EXTENSION vector` + the HNSW index

Both `01` and `02` end with an `information_schema` SELECT that prints the
created columns, so you can confirm without a separate query.

---

## Step 4 — Create and deploy the app

```bash
export DATABRICKS_CONFIG_PROFILE=DEFAULT
APP=weather-vector-app
WS=/Workspace/Users/epatlan1742@sdsu.edu/$APP

databricks apps create $APP

# Push the source into the workspace (respects .gitignore, so .env and .venv
# are never uploaded).
databricks sync --full . $WS

databricks apps deploy $APP --source-code-path $WS
```

### Add the secret resource

The app reads `LAKEBASE_URL` from an app **resource**, which has to be attached
in the UI: **Apps → `weather-vector-app` → Edit → Resources → Add resource → Secret**

| field | value |
|---|---|
| Resource key | `weather-lakebase-url` |
| Scope | `database` |
| Key | `weather-lakebase-url` |
| Permission | `READ` |

The resource key must be exactly `weather-lakebase-url` — that string is what
`app.yaml`'s `valueFrom` looks up. Redeploy after adding it.

If the resource is missing, `LAKEBASE_URL` resolves to an empty string and
`lakebase.py` falls through to reading `database/weather-lakebase-url` through
the SDK — which works only if the app's service principal has READ on the scope.
The resource is the reliable path.

---

## Step 5 — Verify the deployment

```bash
databricks apps get weather-vector-app     # grab the URL, wait for SUCCEEDED
URL=$(databricks apps get weather-vector-app -o json | python3 -c 'import sys,json;print(json.load(sys.stdin)["url"])')
```

The app URL is **not public** — requests need your Databricks identity, which is
why `test_deployment.py` attaches SDK auth headers:

```bash
python test_deployment.py "$URL"      # expect: 26 passed, 0 failed
```

Or drive it by hand (in a browser, already authenticated):

```
$URL/healthz
$URL/weather/stats
$URL/weather/search?query=flash+flood+risk&top_k=5
```

Then populate it:

```bash
curl -s -XPOST "$URL/weather/sync" -H 'content-type: application/json' \
  -d '{"locations":["Chicago, IL","Austin, TX","Miami, FL"],"limit":50}'
```

### Embedding job

`/weather/sync` only stores documents. The vectors come from a separate job, run
from anywhere that can reach the database:

```bash
LAKEBASE_URL='postgresql://...' python notebooks/ingest_weather_embeddings.py
```

It preflights both tables and the vector dimension before doing any work, and
the anti-join means re-running only embeds what's new.

To run it inside Databricks instead, attach it as a notebook task on a
single-node cluster; it needs `psycopg2-binary`, `fastembed`, and
`databricks-sdk`. Sync alerts every 30–60 minutes and re-run the embed job right
after — active alerts expire, so a stale corpus goes quiet.

---

## Teardown

```bash
databricks apps delete weather-vector-app
databricks database delete-database-instance weather-vector-db   # stops billing
databricks secrets delete-secret database weather-lakebase-url
```

Deleting the instance is what stops the charge; deleting the app alone does not.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| App starts, `/healthz` OK, everything else 500 | Secret resource not attached, or the DSN is wrong. Check the app's logs — `lakebase.py` logs which source the URL came from, never the value |
| `password authentication failed` | Usually a doubled hostname, not a bad password. `nslookup` the host from the DSN |
| `permission denied for schema public` | `sql/00_grant_app_role.sql` hasn't been run as a superuser |
| `type "vector" does not exist` | `sql/02` hasn't been run — `CREATE EXTENSION vector` is its first statement |
| `409` from `/weather/search` | `weather_embeddings` doesn't exist; run `sql/02` |
| Search returns `[]` after a successful sync | Documents are stored but not embedded — run the ingest job |
| `PRINCIPAL_DOES_NOT_EXIST` on the grant | SQL was run in a Unity Catalog editor instead of the Lakebase query editor |
| Deploy succeeds but the app won't start | Check `app.yaml`'s port handling — `DATABRICKS_APP_PORT` must win, and it does in `app.py`'s `__main__` block |
