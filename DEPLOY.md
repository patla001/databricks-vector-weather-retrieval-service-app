# Deploying to Databricks Apps

Runbook for `dbc-7e085092-52e4.cloud.databricks.com`. **This has been run
end to end** — the values below are what was actually deployed, not a template.

**Step 1 is the only step that starts billing** (and the app compute in step 4
bills separately). Everything is CLI-driven; no UI step is required.

---

## What is deployed

| | |
|---|---|
| Workspace | `https://dbc-7e085092-52e4.cloud.databricks.com` |
| Lakebase instance | `weather-vector-db` — CU_1, PG 16.14, pgvector 0.8.0, native login on |
| Instance host | `ep-noisy-brook-d1569dvn.database.us-west-2.cloud.databricks.com` |
| Postgres role | `weather_app` (static password, `USAGE` + `CREATE` on `public`) |
| Secret | `database/weather-lakebase-url` |
| App | `weather-vector-app` |
| App URL | `https://weather-vector-app-2808874854650870.aws.databricksapps.com` |
| Verification | `test_deployment.py` — **28 passed, 0 failed** |

> ⚠️ **The two pre-existing apps (`lakebase-support-app`, `massive-lakebase-app`)
> point at a database that no longer exists** — `massive-sync-db` was deleted, and
> its hostname does not resolve. That is exactly why this app uses its own secret
> key rather than the shared `database/lakebase-url`: creating a database here
> never silently repoints the day-2 app at a database missing all of its tables.

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

The app reads `LAKEBASE_URL` from an app **resource**. This is settable from the
CLI — `apps update --json` accepts the full request body including `resources`,
so no UI step is required:

```bash
databricks apps update weather-vector-app --json '{
  "name": "weather-vector-app",
  "resources": [
    {
      "name": "weather-lakebase-url",
      "secret": {"scope": "database", "key": "weather-lakebase-url", "permission": "READ"}
    }
  ]
}'
```

The resource `name` must be exactly `weather-lakebase-url` — that string is what
`app.yaml`'s `valueFrom` looks up.

Also grant the app's service principal READ on the scope, so the SDK fallback in
`lakebase.py` works if the resource is ever detached:

```bash
SP=$(databricks apps get weather-vector-app -o json \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["service_principal_client_id"])')
databricks secrets put-acl database "$SP" READ
```

Redeploy after either change.

The same resource can be attached in the UI instead
(**Apps → app → Edit → Resources → Add resource → Secret**), but the CLI form
above is scriptable and is what was actually used.

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

`/weather/sync` only stores documents. The vectors come from a separate step,
runnable from anywhere that can reach the database:

```bash
LAKEBASE_URL='postgresql://...' python notebooks/ingest_weather_embeddings.py
```

It preflights both tables and the vector dimension before doing any work, and
the anti-join means re-running only embeds what's new.

---

## Step 6 — Keeping the corpus fresh

Active NWS alerts expire within hours, so a corpus populated once goes quiet by
the next day. The refresh runs **inside the app process** on a timer.

| | |
|---|---|
| Where | `weather_scheduler.py`, started at import by `app.py` |
| Cadence | `WEATHER_REFRESH_MINUTES` (default **30**; `0` disables) |
| Each cycle | harvest → upsert → purge alerts expired > `WEATHER_PURGE_EXPIRED_DAYS` → embed pending |
| Observe | `GET /weather/refresh/status` |
| Force one | `POST /weather/refresh` (409 if a cycle is already running) |

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$URL/weather/refresh/status"
# {"enabled":true,"interval_minutes":30,"cycles":1,"failures":0,
#  "last_result":{"fetched":133,"upserted":133,"embeddings_invalidated":2,
#                 "embedded_written":5,"elapsed_seconds":1.96}, ...}
```

A cycle takes ~2s. Every step is idempotent — documents upsert on their natural
id, chunks collide on a derived primary key — so a duplicate or overlapping
cycle wastes work but cannot corrupt anything. A lock skips a tick rather than
stacking cycles, and the thread swallows its exceptions so a failed refresh
degrades freshness without taking the API down.

### Why not a Databricks Job

The obvious design is a scheduled Job, and `notebooks/scheduled_weather_refresh.py`
still supports it (`--skip-embed` splits the stages; `--app-url` drives the app's
endpoints instead of the database). It is **not** what runs here, for two reasons
found by testing rather than assumption:

1. **This workspace is serverless-only** — creating a job with a classic cluster
   fails with `Only serverless compute is supported in the workspace`.
2. **A serverless task that loads `requests` + `psycopg2` + `fastembed` into one
   kernel segfaults it.** The failure is `Fatal error: The Python kernel is
   unresponsive` with the run's logs discarded, which makes it look like a
   hang rather than a crash. Isolated probes established that each *pair* is
   fine — `fastembed` alone, and `psycopg2` + `requests` + `databricks-sdk`
   together, both ran clean — and that the harvest crashed before writing a
   single row.

Driving the app's HTTP endpoints from a job avoids the crash but hits a second
wall: a job's SDK credential is `auth_type: runtime`, an internal token the
Apps OAuth proxy rejects. It answers with the **Databricks Sign-In page as
HTTP 200 `text/html`**, so `raise_for_status()` passes and the failure only
surfaces as a confusing `JSONDecodeError`. `refresh_via_app()` now detects a
non-JSON content-type and says so plainly. Making that path work needs a
service principal with an OAuth M2M secret granted `CAN_USE` on the app —
worth doing if you want the refresh outside the app, but it is strictly more
moving parts than a timer in a process that already works.

The app container runs `psycopg2` + `fastembed` together happily — it must, to
serve `/weather/search` — which is exactly why the scheduler lives there.

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
| `/healthz` returns 200 with an **empty body** on the deployed app | Expected. Databricks Apps claims `/healthz` as its own platform probe and answers it at the proxy — the request never reaches Flask, so you get `content-length: 0` and no `content-type`. Locally the same path returns `{"status":"ok"}` from `app.py`. Use `GET /` to confirm the app itself is serving |
| App starts, `/healthz` OK, everything else 500 | Secret resource not attached, or the DSN is wrong. Check the app's logs — `lakebase.py` logs which source the URL came from, never the value |
| `password authentication failed` | Usually a doubled hostname, not a bad password. `nslookup` the host from the DSN |
| `permission denied for schema public` | `sql/00_grant_app_role.sql` hasn't been run as a superuser |
| `type "vector" does not exist` | `sql/02` hasn't been run — `CREATE EXTENSION vector` is its first statement |
| `409` from `/weather/search` | `weather_embeddings` doesn't exist; run `sql/02` |
| Search returns `[]` after a successful sync | Documents are stored but not embedded — run the ingest job |
| `PRINCIPAL_DOES_NOT_EXIST` on the grant | SQL was run in a Unity Catalog editor instead of the Lakebase query editor |
| Deploy succeeds but the app won't start | Check `app.yaml`'s port handling — `DATABRICKS_APP_PORT` must win, and it does in `app.py`'s `__main__` block |
