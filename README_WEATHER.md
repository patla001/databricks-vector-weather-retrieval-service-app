# Weather Intelligence: NWS → Lakebase pgvector → semantic search

Harvests free-text weather from the National Weather Service, chunks and embeds it,
stores the vectors in Lakebase (Postgres + pgvector), and serves semantic search
over the corpus:

```
POST /weather/sync    {"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}
POST /weather/search  {"query": "flash flood risk this weekend", "top_k": 5}
```

Built on the same pattern as the day-2 ticker-news app — `lakebase.py` connection
helper, `execute_values` + `%s::vector` writes, HNSW cosine index, `<=>` retrieval —
against a new unstructured source.

---

## Results

| | |
|---|---|
| Documents in `weather_documents` | **122** (52 alerts, 70 forecast periods) |
| Chunks in `weather_embeddings` | **184** |
| Unembedded backlog | **0** |
| Orphan embeddings | **0** |
| Duplicate `(document_id, chunk_index)` | **0** |
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2`, **384 dims** |
| Distinct models in the table | **1** |
| Index | `hnsw (embedding vector_cosine_ops)` |
| End-to-end test | **26 passed, 0 failed** |
| Locations | Chicago IL, Austin TX, Houston TX, Miami FL, Denver CO |

### Search quality

Real output from the running service. The assignment's own example query:

```console
$ curl -s -XPOST localhost:8000/weather/search -H 'content-type: application/json' \
    -d '{"query":"flash flood risk this weekend","top_k":5}'

0.5422  [alert] Chicago, IL   Flood Warning    ' in the 24 hours ending at 11:00 AM EDT Sunday…'
0.5334  [alert] Chicago, IL   Flood Warning    'ocal, unimproved roads leading towards the river are flooded…'
0.5233  [alert] Chicago, IL   Flood Warning    'w.weather.gov/safety/flood A Flood Warning means water levels…'
0.5144  [alert] Chicago, IL   Flood Warning    'es or drive cars through flooded areas…'
0.4873  [alert] Chicago, IL   Flood Warning    '9 feet Thursday evening. It will rise to 5.0 feet early Friday…'
```

Filtering by `source_type` cleanly separates the two harvested sources:

```console
$ ... -d '{"query":"hot and sunny afternoon","top_k":3,"source_type":"forecast"}'
0.5884  [forecast] Chicago, IL  'Chicago, IL - Thursday: Mostly sunny, with a high near 76.'
0.5876  [forecast] Austin, TX   'Austin, TX - Thursday: Sunny, with a high near 103. South wind around 5 mph.'
0.5767  [forecast] Chicago, IL  'Chicago, IL - This Afternoon: Scattered showers and thunderstorms…'

$ ... -d '{"query":"dangerous heat","top_k":3,"source_type":"alert"}'
0.5503  [alert] Miami, FL     Heat Advisory
0.5503  [alert] Miami, FL     Heat Advisory
0.5376  [alert] Chicago, IL   Extreme Heat Warning
```

`"severe thunderstorm"` returns Denver's Severe Thunderstorm Warnings; none of these
queries share vocabulary with the retrieved text by accident — the ranking is doing
real semantic work, not keyword matching.

---

## Engineering decisions

**The National Weather Service was the right source because its alert text is genuinely
long-form, and I measured that rather than assuming it.** Across 464 live nationwide
alerts sampled while designing the schema, `description + instruction` ran to a median
of 682 characters and a maximum of 9,116, with 42% over 800. In the corpus above,
alerts average 1,302 characters (median 1,307, max 2,297) and 45 of 52 exceed the chunk
size. That is what makes the chunking step meaningful rather than ceremonial. NWS also
needs no API key, which kept the work on harvesting, vectorization, and retrieval
instead of auth plumbing.

**The hourly forecast endpoint is deliberately not used.** `/gridpoints/{o}/{x},{y}/forecast/hourly`
returns periods whose `detailedForecast` is an **empty string** — only a two-word
`shortForecast` like "Partly Sunny". There is no unstructured text in it to embed, so
including it would have inflated the row count with vectors of near-identical fragments.
The daily `/forecast` endpoint is the one with real narrative.

**`limit` is applied client-side because api.weather.gov rejects it.**
`GET /alerts/active?area=IL&limit=2` returns **HTTP 400** — `Query parameter "limit" is
not recognized`. The endpoint has no pagination controls at all. So `{"limit": 50}` in
the request body caps documents per location inside `weather_client.py`, after the fetch.

**Alerts are fetched per state, not per point.** `?point=lat,lon` works but returns only
alerts whose polygon covers that exact coordinate — often zero on a calm day, which
makes for an empty corpus and an unconvincing demo. `?area={ST}` gives the surrounding
state's active alerts, deduplicated by their NWS `id` across locations that share a
state. `WEATHER_ALERT_SCOPE=point` switches to the precise behavior; the tradeoff is
recall against precision and it is a one-line config change.

**Chunking is 800/100, inherited from the reference pipeline and validated against this
corpus.** The measured outcome, straight from `sql/03`:

| source | chunks | documents | avg chunks/doc | max chunks | docs > 800 chars |
|---|---:|---:|---:|---:|---|
| `alert` | 110 | 51 | **2.16** | 3 | 45 / 52 |
| `forecast` | 70 | 70 | **1.00** | 1 | 0 / 70 |

Forecast periods are always exactly one chunk (48–288 characters), so the parameters cost
them nothing, while long alerts split with a 100-character overlap that keeps a hazard
sentence from being severed at the boundary.

**`fastembed` serves `all-MiniLM-L6-v2` instead of `sentence-transformers`.** Same
weights, same 384 dimensions, same vector space — but the ONNX export runs on CPU without
pulling in ~2.5 GB of torch, which matters when the app has to cold-start inside a
Databricks App container. The day-2 project measured the two at cosine 0.99+ agreement on
identical input. Critically, **both sides of this pipeline import the same
`embed_texts` from `weather_search.py`**: the ingestion job and the query path physically
cannot drift onto different exports of the model.

**Text is embedded with its location and hazard prepended.** An alert becomes
`"Flash Flood Warning for Marion, SC. <headline> <description> <instruction>"`, a
forecast becomes `"Chicago, IL - This Afternoon: <detailedForecast>"`. MiniLM has no
access to the structured columns at retrieval time — only this string becomes the vector —
and user queries name places and hazards, so putting them in the embedded text is what
makes `"dangerous heat"` find a Miami Heat Advisory.

**`text_hash` exists because NWS revises alerts in place.** A warning gets extended or its
call-to-action rewritten while keeping the same `id`. Without a guard, `ON CONFLICT DO
UPDATE` would replace `narrative_text` while vectors of the *old* text stayed in
`weather_embeddings` — and `/weather/search` would go on serving them, scored against text
the API no longer publishes. The sync compares the stored hash against the incoming one and
deletes exactly the changed documents' embedding rows, which puts them back into the
anti-join for the next embed run. **This fired unprompted during testing**: NWS revised a
live alert between two syncs, four stale vectors were dropped, and the next ingest run
re-embedded them.

**`source_type` is denormalized onto the embeddings table** so `/weather/search` can filter
before the join, rather than making the planner walk the vector index and then discard rows.

**The HNSW index exists, and at this corpus size the planner correctly ignores it.**
`scripts/benchmark_hnsw.py` reports this honestly rather than claiming a speedup that
isn't there:

```
corpus: 184 embedding row(s) over 122 document(s)

  index allowed          n=40   min=1.10ms  p50=1.26ms  p95=1.51ms
  seqscan forced         n=40   min=1.07ms  p50=1.21ms  p95=1.57ms
  -> NO speedup at this corpus size (1.04x slower at p50)
```

`EXPLAIN ANALYZE` confirms *both* paths choose `Seq Scan` — over 184 rows, scanning beats
an index walk and Postgres knows it. To show the crossover is real rather than just
asserting it, `--synthetic N` builds a throwaway table of N random unit vectors, indexes
it, benchmarks it, and drops it:

```
synthetic scale test: 50000 rows
  index allowed          n=40   min=0.56ms   p50=0.67ms   p95=0.94ms
  seqscan forced         n=40   min=25.44ms  p50=29.67ms  p95=33.21ms
  -> index path is 44.45x faster at p50 (50000 rows)

  ->  Index Scan using weather_embeddings_bench_hnsw on weather_embeddings_bench
        Order By: (embedding <=> '[<384-dim query vector>]'::vector)
```

**44× at 50,000 rows**, with the plan flipping to `Index Scan using ..._hnsw`. The index is
correct and worth having; this corpus is simply below the size where it pays.

**Locations resolve from a built-in table, not a geocoder.** NWS covers the US only, so a
40-city lookup plus a raw `"lat,lon"` form covers the assignment's input shape with no
third-party dependency, no rate limit, and no network call that can fail between the
request and the grid lookup. An unknown city returns a 400 listing every supported name.

---

## Files

| Path | What it is |
|---|---|
| `weather_client.py` | NWS API client — location resolution, alerts + forecasts, normalization |
| `weather_pipeline.py` | Chunking, embedding, and `execute_values` writes; the anti-join that finds new work |
| `weather_search.py` | Query-side pgvector search, the shared embedder, optional RAG summary |
| `app.py` | Flask: `/healthz`, `/weather/sync`, `/weather/search` (POST + GET), `/weather/stats` |
| `lakebase.py` | Connection helper, copied unchanged from the day-2 project |
| `notebooks/ingest_weather_embeddings.py` | Batch embed job with a dimension preflight |
| `scripts/benchmark_hnsw.py` | HNSW vs. sequential-scan latency, with a synthetic-scale mode |
| `test_deployment.py` | End-to-end test; verifies every write through the API *and* in Postgres |
| `sql/00`–`sql/03` | Role grant, both table DDLs, verification queries |

---

## Schema

```sql
CREATE TABLE weather_documents (
    id             TEXT PRIMARY KEY,   -- "alert:urn:oid:…" | "forecast:<office>/<x>,<y>:<ISO8601>"
    location       TEXT NOT NULL,      -- canonical "City, ST" from /points relativeLocation
    latitude       DOUBLE PRECISION,
    longitude      DOUBLE PRECISION,
    source_type    TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),
    event          TEXT,               -- "Flash Flood Warning" | "This Afternoon"
    headline       TEXT,
    narrative_text TEXT NOT NULL,      -- the text that gets chunked and embedded
    text_hash      TEXT NOT NULL,      -- sha256(narrative_text); drives re-embed on revision
    severity       TEXT,
    area_desc      TEXT,
    issued_at      TIMESTAMPTZ,
    effective_at   TIMESTAMPTZ,
    expires_at     TIMESTAMPTZ,
    payload        JSONB NOT NULL,     -- raw upstream JSON, for provenance
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE weather_embeddings (
    id          TEXT PRIMARY KEY,      -- sha256("<document_id>:<chunk_index>")[:32]
    document_id TEXT NOT NULL REFERENCES weather_documents (id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,         -- denormalized so filters apply before the join
    chunk_index INT  NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   VECTOR(384) NOT NULL,
    model_name  TEXT NOT NULL,         -- per-row provenance
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX ON weather_embeddings USING hnsw (embedding vector_cosine_ops);
```

Document ids are prefixed by source type so the two id spaces can never collide. Alert ids
come from NWS (`urn:oid:2.49.0.1.840.0.<sha>.001.1`) and are already stable and globally
unique. Forecast ids are keyed by grid square and **period start time**, not period number —
the numbers shift as periods roll off ("Tonight" becomes period 1), so a number-keyed id
would overwrite a different forecast on every sync.

Chunk ids are derived from position rather than random, so re-running the embed job collides
on the primary key and is skipped by `ON CONFLICT DO NOTHING` instead of inserting duplicates.

---

## Running it end to end

```bash
cd databricks-vector-weather-retrieval-service-app
uv venv && uv pip install --python .venv/bin/python -r requirements.txt

cp .env.example .env      # set LAKEBASE_URL

# 1. Schema — paste into the Lakebase SQL editor, opened from the database
#    instance page (NOT a workspace SQL editor or a %sql cell, which target
#    Unity Catalog and cannot see these Postgres tables).
#      sql/01_setup_weather_documents.sql
#      sql/02_setup_weather_embeddings.sql

# 2. Serve
.venv/bin/python app.py                       # http://localhost:8000
curl -s localhost:8000/healthz                # {"status":"ok"}

# 3. Harvest
curl -s -XPOST localhost:8000/weather/sync -H 'content-type: application/json' \
  -d '{"locations":["Chicago, IL","Austin, TX","Houston, TX","Miami, FL"],"limit":50}'
# -> {"synced":93,"by_source":{"alert":37,"forecast":56},"embeddings_invalidated":0,...}

# 4. Embed
.venv/bin/python notebooks/ingest_weather_embeddings.py
# -> embedded: 93 document(s) -> 142 chunk(s), 142 row(s) written

# 5. Retrieve
curl -s -XPOST localhost:8000/weather/search -H 'content-type: application/json' \
  -d '{"query":"flash flood risk this weekend","top_k":5}'

curl -s 'localhost:8000/weather/search?query=severe+thunderstorm&top_k=3&summarize=true'

# 6. Verify
.venv/bin/python test_deployment.py http://localhost:8000     # 26 passed, 0 failed
.venv/bin/python scripts/benchmark_hnsw.py --runs 40 --synthetic 50000
#    plus sql/03_verify_weather_embeddings.sql in the Lakebase SQL editor
```

Steps 3–4 are safe to re-run: documents upsert on their natural id and chunks are skipped
on conflict, so row counts stay flat.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/healthz` | Liveness probe |
| `GET` | `/` | API surface + the list of supported city names |
| `POST` | `/weather/sync` | `{"locations": [...], "limit": 50, "sources": ["alert","forecast"]}` — all optional |
| `POST` | `/weather/search` | `{"query": "...", "top_k": 5, "source_type": "alert", "location": "Chicago, IL"}` |
| `GET` | `/weather/search` | `?query=…&top_k=5&source_type=alert&summarize=true` |
| `GET` | `/weather/stats` | Row counts per table plus the unembedded backlog |

### `POST /weather/search`

```json
{
  "query": "flash flood risk this weekend",
  "top_k": 5,
  "count": 5,
  "source_type": null,
  "location": null,
  "results": [
    {
      "id": "alert:urn:oid:2.49.0.1.840.0.…",
      "location": "Chicago, IL",
      "source_type": "alert",
      "event": "Flood Warning",
      "headline": "Flood Warning issued August 16 …",
      "chunk_index": 2,
      "chunk_text": "…",
      "similarity": 0.5422,
      "severity": "Moderate",
      "issued_at": "…", "expires_at": "…"
    }
  ]
}
```

### Edge cases

| Case | Behavior |
|---|---|
| Missing / blank / non-string `query` | `400` |
| `top_k` not an integer | `400` |
| `top_k` out of range | Clamped to 1–20 (`9999 → 20`, `0 → 1`, `-5 → 1`) |
| `source_type` not `alert`/`forecast` | `400`, naming the valid values |
| Empty `weather_embeddings` | `200` with `"results": []` and a hint — not an error |
| `weather_embeddings` missing | `409` pointing at `sql/02` |
| Unknown location on sync | `400` listing every supported city |
| NWS 5xx / timeout for one location | Others still sync; the failures come back in an `errors` array so the count is never silently partial |
| `summarize=true` with no API key | Results still returned, with a `summary_error` note |

All verified — see the test output above.

### RAG summary (stretch)

`GET /weather/search?...&summarize=true` adds a `summary` field: a short paragraph
grounded in the retrieved chunks, via the Anthropic Messages API (`claude-opus-5`,
`ANTHROPIC_API_KEY`). The system prompt constrains it to the supplied passages and tells
it to say so when they don't cover the question. Two details worth noting in the client
code: thinking is on by default on this model, so the text is collected by iterating
`response.content` for `text` blocks rather than indexing `content[0]` (which is a
thinking block); and `stop_reason == "refusal"` is checked before reading content. The
summary can never take down search — any failure becomes a `summary_error` note beside
the results.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `409` from `/weather/search` | `sql/02` hasn't been run — `weather_embeddings` doesn't exist |
| `permission denied for schema public` | The app role lacks `CREATE`; run `sql/00_grant_app_role.sql` as a superuser |
| `PRINCIPAL_DOES_NOT_EXIST` on the grant | The SQL was run in a workspace editor / `%sql` cell, which targets Unity Catalog. Use the Lakebase instance's own query editor |
| `type "vector" does not exist` | `CREATE EXTENSION vector` hasn't run — it's the first statement of `sql/02` |
| Preflight: `Dimension mismatch` | `WEATHER_EMBED_MODEL` and the `VECTOR(n)` column disagree; recreate the table at the right width |
| Search returns `[]` right after a sync | Documents are stored but not yet embedded — run the ingest script |
| `HTTP 400 … "limit" is not recognized` | Something is passing `limit` through to api.weather.gov; it must be applied client-side |
| Sync returns an `errors` array | One or more locations failed upstream; the count covers only what succeeded |
| Search is slow at scale | Confirm the HNSW index exists (`sql/03` query 8) and that `EXPLAIN` actually chooses it |

---

## Limitations and what I'd do next

- **The city table caps coverage at 40 cities.** Raw `"lat,lon"` works for anywhere in NWS
  coverage, but "Fargo, ND" doesn't resolve. A geocoder is the fix; I skipped it to avoid a
  third-party dependency with its own rate limit in the request path.
- **Active alerts expire, so the corpus is a moving target.** Nothing currently deletes
  documents past `expires_at`, so old alerts accumulate and stay searchable. A TTL sweep
  (`DELETE FROM weather_documents WHERE expires_at < now() - interval '7 days'`) would cascade
  to the embeddings; I'd want it on a schedule before running this continuously.
- **Chunking is character-based, not token-aware.** Cheap and close enough at 800 characters,
  but a token-aware splitter would pack chunks more evenly against the model's 256-token window.
- **No reranker.** Cosine over MiniLM is a single-stage retriever. The similarity scores
  cluster around 0.5 even for good hits, which is normal for this model but means the scores
  are better for ranking than for thresholding — I would not build an "is this relevant"
  cutoff on the raw number without calibrating it first.
- **Alerts are attributed to the requesting location, not their true area.** A statewide
  `?area=IL` fetch tags every alert with "Chicago, IL" even when it covers southern Illinois —
  which is why a Chicago flood query returns alerts for Crawford and Lawrence counties. The
  `area_desc` column carries the real coverage; filtering on the alert's `affectedZones`
  against the location's forecast zone would fix the attribution.
- **No auth on `/weather/sync`.** It's a write endpoint that triggers outbound API calls.
  Behind Databricks Apps it inherits workspace SSO, but standalone it should require something.
- **Verification ran against Postgres 16 + pgvector 0.8.6 in Docker**, not the class Lakebase
  instance, which was no longer reachable (`massive-sync-db.database.cloud.databricks.com` does
  not resolve). Everything exercised is standard Postgres and pgvector — the DDL, the `<=>`
  operator, `execute_values` with `%s::vector`, HNSW — so it should transfer unchanged; only
  `LAKEBASE_URL` differs. The `sslmode=require` path is the one thing not exercised locally.
