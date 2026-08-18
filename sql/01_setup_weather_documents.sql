-- RUN IN THE LAKEBASE SQL EDITOR (from the database instance page) - NOT in a
-- workspace SQL editor or `%sql` cell, which target Unity Catalog and cannot
-- see these Postgres tables.
--
-- Setup script for the weather_documents table: the RAW document store that
-- POST /weather/sync writes into, and that the embedding job
-- (notebooks/ingest_weather_embeddings.py) reads from.
--
-- app.py mirrors this DDL in ensure_weather_documents_table() so a fresh deploy
-- self-heals. This file is the readable source of truth, and the only way to
-- create the table if the app role lacks CREATE (see 00_grant_app_role.sql).

CREATE TABLE IF NOT EXISTS weather_documents (
    -- "alert:urn:oid:2.49.0.1.840.0.<sha>.001.1" for alerts (the NWS id is
    -- already stable and globally unique), or
    -- "forecast:<office>/<x>,<y>:<period start ISO8601>" for forecast periods.
    -- Prefixing with the source type keeps the two id spaces from ever colliding.
    id             TEXT PRIMARY KEY,

    -- Canonical "City, ST" taken from the /points response's relativeLocation,
    -- so "chicago, il" and "41.88,-87.63" both land on the same label.
    location       TEXT NOT NULL,
    latitude       DOUBLE PRECISION,
    longitude      DOUBLE PRECISION,
    -- How latitude/longitude were decided: 'polygon' (the alert's own
    -- footprint), 'zone' (centroid of the NWS zones it covers), 'state', or
    -- 'point' (a forecast grid point, or an alert with nothing better). Four
    -- alerts in five ship no polygon, so without this the map cannot tell an
    -- exact footprint from an approximation - and earlier builds silently
    -- plotted every zone-based alert on whichever city requested it.
    geo_source     TEXT,

    source_type    TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),

    -- "Flash Flood Warning" for alerts; the period name ("This Afternoon",
    -- "Tuesday Night") for forecasts.
    event          TEXT,
    headline       TEXT,

    -- The free text that actually gets chunked and embedded.
    narrative_text TEXT NOT NULL,

    -- sha256 of narrative_text. NWS revises alerts in place under a stable id,
    -- so an upsert can change the text out from under embeddings that already
    -- exist. The sync compares this hash and deletes the stale embedding rows
    -- when it changes, which puts the document back into the re-embed anti-join.
    -- Without it, /weather/search silently serves vectors of text that is gone.
    text_hash      TEXT NOT NULL,

    severity       TEXT,
    area_desc      TEXT,

    issued_at      TIMESTAMPTZ,
    effective_at   TIMESTAMPTZ,
    expires_at     TIMESTAMPTZ,

    -- Raw upstream JSON, kept for provenance: every derived column above can be
    -- recomputed from this without re-hitting the API.
    payload        JSONB NOT NULL,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_weather_documents_location
ON weather_documents (location);

CREATE INDEX IF NOT EXISTS idx_weather_documents_source_type
ON weather_documents (source_type);

-- Supports "what came in most recently" browsing and TTL sweeps of expired alerts.
CREATE INDEX IF NOT EXISTS idx_weather_documents_issued_at
ON weather_documents (issued_at DESC);

-- Verify the table was created
SELECT
    table_name,
    column_name,
    data_type
FROM information_schema.columns
WHERE table_name = 'weather_documents'
ORDER BY ordinal_position;
