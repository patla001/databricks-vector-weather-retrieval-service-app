-- RUN IN THE LAKEBASE SQL EDITOR (from the database instance page) - NOT in a
-- workspace SQL editor or `%sql` cell, which target Unity Catalog and cannot
-- see these Postgres tables.
--
-- Setup script for weather_embeddings. Run 01_setup_weather_documents.sql first:
-- the foreign key below will not resolve otherwise.
--
-- Unlike weather_documents, the app does NOT create this table on the fly.
-- CREATE EXTENSION and index builds are privileged, one-time DDL, and doing them
-- per-request would be both slow and likely to fail on a least-privilege role.

CREATE EXTENSION IF NOT EXISTS vector;

-- VECTOR(384) matches sentence-transformers/all-MiniLM-L6-v2, the model named by
-- WEATHER_EMBED_MODEL. If you change the model, change this dimension to match
-- and recreate the table - a mismatch fails at INSERT, not at startup:
--   - sentence-transformers/all-MiniLM-L6-v2:  384
--   - sentence-transformers/all-MiniLM-L12-v2: 384
--   - BAAI/bge-small-en-v1.5:                  384
--   - sentence-transformers/all-mpnet-base-v2: 768
--   - BAAI/bge-base-en-v1.5:                   768
--   - BAAI/bge-large-en-v1.5:                 1024
-- notebooks/ingest_weather_embeddings.py preflights this: it reads the column's
-- actual dimension out of pg_attribute and refuses to run on a mismatch.
CREATE TABLE IF NOT EXISTS weather_embeddings (
    -- sha256("<document_id>:<chunk_index>")[:32] - derived from position, not
    -- random, so re-running the embed job collides on the PK instead of
    -- duplicating rows.
    id          TEXT PRIMARY KEY,

    -- ON DELETE CASCADE: dropping a document must not leave orphan vectors that
    -- the search JOIN would silently discard while still counting toward LIMIT.
    document_id TEXT NOT NULL REFERENCES weather_documents (id) ON DELETE CASCADE,

    -- Denormalized from weather_documents so /weather/search can filter by
    -- source_type on the embeddings table itself. Filtering after the join would
    -- force the planner to walk the whole vector index before discarding rows.
    source_type TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),

    chunk_index INT  NOT NULL,
    chunk_text  TEXT NOT NULL,

    embedding   VECTOR(384) NOT NULL,

    -- Per-row provenance: which model produced this vector, and when. Makes a
    -- model migration a filter rather than a full-table guess.
    model_name  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Belt and braces alongside the derived PK: makes the "one vector per
    -- (document, chunk)" invariant a database guarantee rather than a
    -- property of the id function.
    UNIQUE (document_id, chunk_index)
);

-- HNSW with cosine ops, matching the `<=>` operator used by weather_search.py.
-- The operator class must agree with the distance operator or the planner will
-- ignore the index and silently fall back to a sequential scan.
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_embedding
ON weather_embeddings
USING hnsw (embedding vector_cosine_ops);

-- Supports the ON DELETE CASCADE and the stale-embedding cleanup in the sync.
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_document_id
ON weather_embeddings (document_id);

CREATE INDEX IF NOT EXISTS idx_weather_embeddings_source_type
ON weather_embeddings (source_type);

-- Verify the table was created
SELECT
    table_name,
    column_name,
    data_type,
    udt_name
FROM information_schema.columns
WHERE table_name = 'weather_embeddings'
ORDER BY ordinal_position;
