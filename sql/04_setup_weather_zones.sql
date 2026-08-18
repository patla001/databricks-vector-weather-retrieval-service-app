-- RUN IN THE LAKEBASE SQL EDITOR (database instance page, not a workspace editor)
--
-- Zone centroid cache.
--
-- Most active NWS alerts carry no inline polygon: on a 189-alert nationwide
-- sample only 38 (20%) had a `geometry`. The rest are issued against zones and
-- reference them by URL, and a zone polygon is a separate ~26 KB request. There
-- are roughly 3600 zones nationally, far too many to fetch per run - but zones
-- are static geography, so a centroid fetched once is correct forever.
--
-- This table is that cache: two floats per zone, filled lazily by the harvest
-- under a per-run budget. The app creates it automatically (weather_zones.py);
-- this file exists so the schema can be reviewed and applied by hand like the
-- others.

CREATE TABLE IF NOT EXISTS weather_zones (
    zone_id    TEXT PRIMARY KEY,          -- UGC id, e.g. 'OKZ074'
    zone_type  TEXT,                      -- 'forecast' | 'county' | 'marine' | ...
    name       TEXT,
    state      TEXT,
    latitude   DOUBLE PRECISION NOT NULL,
    longitude  DOUBLE PRECISION NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Backfill for a database created before geo_source existed. Harmless to rerun.
ALTER TABLE weather_documents ADD COLUMN IF NOT EXISTS geo_source TEXT;

-- Verify.
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'weather_zones'
ORDER BY ordinal_position;
