"""
Weather embeddings pipeline.

Plain-Python stages shared by the Flask app (POST /weather/sync) and the batch
embedding job (notebooks/ingest_weather_embeddings.py). Keeping the logic here
rather than inline in either means it can be exercised on its own: every stage
takes its embedder as a callable, so a test can pass a stub instead of loading
a model.

Connections reuse lakebase.py, which resolves LAKEBASE_URL from the environment
when set and otherwise from the `database/lakebase-url` secret - so the same
code runs locally off .env and on a cluster off the secret scope.

Writes go through psycopg2 (execute_values, batched, with an explicit ::vector
cast) rather than Spark's JDBC writer, which is not supported against Lakebase.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Callable, Sequence

from psycopg2.extras import execute_values

import lakebase

logger = logging.getLogger(__name__)

# An embedder takes a list of strings and returns one vector per string.
Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]

DEFAULT_DOCUMENTS_TABLE = os.environ.get("WEATHER_DOCUMENTS_TABLE", "weather_documents")
DEFAULT_EMBEDDINGS_TABLE = os.environ.get("WEATHER_EMBEDDINGS_TABLE", "weather_embeddings")

# Character-based, not token-based: cheap, and close enough at this size.
#
# These are the reference pipeline's values, kept because the measurements
# justify them for weather text specifically. Sampling 464 live nationwide
# alerts, `description + instruction` ran to a median of 682 characters and a
# maximum of 9116, with 42% over 800 - so alerts genuinely split, and the 100
# character overlap keeps a hazard sentence from being cut across the boundary.
# Forecast `detailedForecast` text tops out around 260 characters, so a forecast
# period is always exactly one chunk and the parameters cost it nothing.
CHUNK_SIZE = int(os.environ.get("WEATHER_CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.environ.get("WEATHER_CHUNK_OVERLAP", "100"))


def to_vector_literal(values: Sequence[float]) -> str:
    """Render a vector in pgvector's text form: '[0.1,0.2,...]'.

    Paired with an explicit ::vector cast at the insert site. pgvector also
    accepts a double precision[] via an assignment cast, but the native literal
    is unambiguous and avoids depending on that cast existing.
    """
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping fixed-width windows."""
    text = " ".join((text or "").split())
    if not text:
        return []
    if size <= overlap:
        raise ValueError(f"chunk size ({size}) must exceed overlap ({overlap})")
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + size])
        if start + size >= len(text):
            break
        start += size - overlap
    return chunks


def chunk_id(document_id: str, index: int) -> str:
    """Stable primary key for a chunk.

    Derived from position rather than random, so re-running the embed job
    collides on the PK (and is skipped by ON CONFLICT DO NOTHING) instead of
    inserting a second copy of the same vector.
    """
    return hashlib.sha256(f"{document_id}:{index}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Stage 1 - documents
# ---------------------------------------------------------------------------

_DOCUMENT_COLUMNS = (
    "id", "location", "latitude", "longitude", "source_type", "event", "headline",
    "narrative_text", "text_hash", "severity", "area_desc",
    "issued_at", "effective_at", "expires_at", "payload",
)


def upsert_documents(
    documents: list[dict],
    documents_table: str = DEFAULT_DOCUMENTS_TABLE,
    embeddings_table: str = DEFAULT_EMBEDDINGS_TABLE,
) -> dict:
    """Upsert harvested documents, and invalidate embeddings whose text changed.

    Returns {"written": n, "reembed": m}.

    NWS revises alerts in place under a stable id - a warning gets extended, or
    its call-to-action is rewritten - so an upsert can replace narrative_text
    while vectors of the OLD text are still sitting in weather_embeddings. Those
    vectors would keep being returned by /weather/search, scored against text
    the API no longer serves.

    The fix is the text_hash column: the stored hashes for the incoming ids are
    read first, compared against the incoming ones, and the embedding rows of
    just the changed documents are deleted. The anti-join in pending_documents()
    then picks them up on the next embed run. Documents whose text is unchanged
    keep their vectors and cost nothing.

    The comparison is a separate SELECT rather than something clever in the
    upsert because RETURNING cannot see EXCLUDED - it reports the row as it
    stands *after* the update, by which point the old hash is already gone.
    """
    if not documents:
        return {"written": 0, "reembed": 0}

    rows = [
        tuple(
            json.dumps(doc.get(col)) if col == "payload" else doc.get(col)
            for col in _DOCUMENT_COLUMNS
        )
        for doc in documents
    ]

    updatable = [c for c in _DOCUMENT_COLUMNS if c != "id"]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)

    incoming = {doc["id"]: doc.get("text_hash") for doc in documents}

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            # Read the hashes we are about to overwrite, in the same transaction
            # as the upsert so nothing can slip in between.
            cur.execute(
                f"SELECT id, text_hash FROM {documents_table} WHERE id = ANY(%s)",
                (list(incoming),),
            )
            stale = [
                row["id"] for row in cur.fetchall()
                if row["text_hash"] != incoming.get(row["id"])
            ]

            # fetch=True so the ids come back across every page. execute_values
            # splits `rows` into page_size batches and issues one statement per
            # batch, so a plain cur.fetchall()/cur.rowcount afterwards would
            # describe only the final batch and undercount every earlier one.
            returned = execute_values(
                cur,
                f"""
                INSERT INTO {documents_table} ({", ".join(_DOCUMENT_COLUMNS)})
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    {set_clause},
                    synced_at = now()
                RETURNING id
                """,
                rows,
                template="(" + ",".join(["%s"] * (len(_DOCUMENT_COLUMNS) - 1)) + ",%s::jsonb)",
                page_size=200,
                fetch=True,
            )
            written = len(returned)

            reembed = 0
            if stale:
                cur.execute(
                    f"DELETE FROM {embeddings_table} WHERE document_id = ANY(%s)",
                    (stale,),
                )
                reembed = cur.rowcount
                logger.info(
                    "invalidated %d embedding row(s) across %d revised document(s)",
                    reembed, len(stale),
                )
        conn.commit()

    return {"written": written, "reembed": reembed}


# ---------------------------------------------------------------------------
# Stage 2 - embeddings
# ---------------------------------------------------------------------------


def pending_documents(
    documents_table: str = DEFAULT_DOCUMENTS_TABLE,
    embeddings_table: str = DEFAULT_EMBEDDINGS_TABLE,
    source_type: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Documents that have no embedding rows yet.

    An anti-join rather than a full re-embed, so re-running the pipeline only
    does the new work - and, thanks to the invalidation in upsert_documents,
    picks up revised documents as if they were new.
    """
    sql = f"""
        SELECT d.id, d.location, d.source_type, d.event, d.headline,
               d.narrative_text, d.issued_at
        FROM {documents_table} d
        LEFT JOIN {embeddings_table} e ON e.document_id = d.id
        WHERE e.id IS NULL
          AND (%(source_type)s::text IS NULL OR d.source_type = %(source_type)s)
        ORDER BY d.issued_at DESC NULLS LAST
    """
    if limit is not None:
        sql += "\n        LIMIT %(limit)s"
    return lakebase.run_query(sql, {"source_type": source_type, "limit": limit})


def embed_documents(
    embed: Embedder,
    model_name: str,
    documents: list[dict],
    embeddings_table: str = DEFAULT_EMBEDDINGS_TABLE,
    batch_size: int = 64,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
    log: Callable[[str], None] = print,
) -> dict:
    """Chunk, embed and store every pending document.

    Returns {"documents": n, "chunks": m, "written": w}. `written` can be lower
    than `chunks` on a re-run: ON CONFLICT DO NOTHING skips chunks that are
    already stored, which is the desired no-op rather than an error.

    Chunking happens before batching so a single long alert - the worst case
    measured was 9116 characters, about 12 chunks - is embedded in the same
    batches as everything else rather than in one oversized call.
    """
    if not documents:
        log("  nothing to embed")
        return {"documents": 0, "chunks": 0, "written": 0}

    # Flatten to (document, chunk_index, chunk_text) so batching is uniform.
    units: list[tuple[dict, int, str]] = []
    for doc in documents:
        for index, text in enumerate(chunk_text(doc["narrative_text"], chunk_size, chunk_overlap)):
            units.append((doc, index, text))

    if not units:
        log("  documents had no embeddable text")
        return {"documents": len(documents), "chunks": 0, "written": 0}

    written = 0
    for start in range(0, len(units), batch_size):
        batch = units[start:start + batch_size]
        vectors = embed([text for _, _, text in batch])

        rows = [
            (
                chunk_id(doc["id"], index),
                doc["id"],
                doc["source_type"],
                index,
                text,
                to_vector_literal(vector),
                model_name,
            )
            for (doc, index, text), vector in zip(batch, vectors)
        ]

        with lakebase.get_connection() as conn:
            with conn.cursor() as cur:
                # DO NOTHING + RETURNING yields only the rows actually inserted,
                # which is exactly the count we want. fetch=True collects them
                # across every page execute_values issues - cur.rowcount would
                # only describe the last one.
                inserted = execute_values(
                    cur,
                    f"""
                    INSERT INTO {embeddings_table}
                        (id, document_id, source_type, chunk_index, chunk_text,
                         embedding, model_name, created_at)
                    VALUES %s
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id
                    """,
                    rows,
                    template="(%s,%s,%s,%s,%s,%s::vector,%s,now())",
                    page_size=200,
                    fetch=True,
                )
                written += len(inserted)
            conn.commit()

        log(f"  embedded {min(start + batch_size, len(units))}/{len(units)} chunks")

    return {"documents": len(documents), "chunks": len(units), "written": written}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarize(
    documents_table: str = DEFAULT_DOCUMENTS_TABLE,
    embeddings_table: str = DEFAULT_EMBEDDINGS_TABLE,
) -> dict:
    """Row counts for both tables, plus the unembedded backlog."""
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    (SELECT COUNT(*) FROM {documents_table})                          AS documents,
                    (SELECT COUNT(*) FROM {documents_table} WHERE source_type='alert')    AS alerts,
                    (SELECT COUNT(*) FROM {documents_table} WHERE source_type='forecast') AS forecasts,
                    (SELECT COUNT(*) FROM {embeddings_table})                         AS embeddings,
                    (SELECT COUNT(*) FROM {documents_table} d
                       LEFT JOIN {embeddings_table} e ON e.document_id = d.id
                      WHERE e.id IS NULL)                                             AS pending
            """)
            return dict(cur.fetchone())


# ---------------------------------------------------------------------------
# Map view
# ---------------------------------------------------------------------------

# Coordinates are rounded before they go over the wire. NWS publishes alert
# polygons at 2-4 decimal places; 3 is ~110 m, far below one pixel at any globe
# zoom the UI offers, and it cuts the payload roughly in half.
_MAP_COORD_PRECISION = 3


def _thin_ring(ring: list, precision: int = _MAP_COORD_PRECISION) -> list:
    """Round a GeoJSON ring and drop points that round onto their neighbour."""
    out = []
    for point in ring:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            lon = round(float(point[0]), precision)
            lat = round(float(point[1]), precision)
        except (TypeError, ValueError):
            continue
        if out and out[-1] == [lon, lat]:
            continue
        out.append([lon, lat])
    # A ring needs 4 points (first == last) to be a polygon at all. Below that,
    # return nothing and let the caller fall back to the point marker.
    return out if len(out) >= 4 else []


def thin_geometry(geometry) -> dict | None:
    """Shrink a GeoJSON Polygon/MultiPolygon for transport; None if unusable.

    Only the outer ring survives. NWS alert polygons are simple hazard
    footprints with no holes, and drawing interiors on a sphere buys nothing
    the outline does not already say.
    """
    if not isinstance(geometry, dict):
        return None
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return None

    if kind == "Polygon":
        rings = [_thin_ring(coords[0])] if coords else []
    elif kind == "MultiPolygon":
        rings = [_thin_ring(poly[0]) for poly in coords if poly]
    else:
        return None

    rings = [r for r in rings if r]
    if not rings:
        return None
    return {"type": "MultiPolygon", "rings": rings}


def map_features(
    source_type: str | None = None,
    include_expired: bool = False,
    limit: int = 1000,
    documents_table: str = DEFAULT_DOCUMENTS_TABLE,
) -> list[dict]:
    """Documents shaped for the globe: geography, labels, no narrative body.

    narrative_text is deliberately left out - it is the bulk of a document and
    the map only needs it once the user opens one, which GET /weather/document
    serves. Sending it for every feature would multiply this response several
    times over for text nothing on screen is showing.

    Expired alerts are hidden by default. An alert whose expires_at has passed
    is not a current hazard, and plotting it implies otherwise.
    """
    rows = lakebase.run_query(
        f"""
        SELECT id, location, latitude, longitude, source_type, event, headline,
               severity, area_desc, issued_at, expires_at,
               payload -> 'geometry' AS geometry
        FROM {documents_table}
        WHERE (%(source_type)s::text IS NULL OR source_type = %(source_type)s)
          AND (%(include_expired)s OR expires_at IS NULL OR expires_at > now())
        ORDER BY
            -- Severe things first, so a truncated response keeps what matters.
            CASE severity WHEN 'Extreme' THEN 0 WHEN 'Severe' THEN 1
                          WHEN 'Moderate' THEN 2 WHEN 'Minor' THEN 3 ELSE 4 END,
            issued_at DESC NULLS LAST
        LIMIT %(limit)s
        """,
        {
            "source_type": source_type,
            "include_expired": include_expired,
            "limit": max(1, min(int(limit), 5000)),
        },
    )

    features = []
    for row in rows:
        feature = dict(row)
        feature["geometry"] = thin_geometry(feature.get("geometry"))
        if feature["latitude"] is not None:
            feature["latitude"] = round(float(feature["latitude"]), 4)
        if feature["longitude"] is not None:
            feature["longitude"] = round(float(feature["longitude"]), 4)
        features.append(feature)
    return features


def get_document(doc_id: str, documents_table: str = DEFAULT_DOCUMENTS_TABLE) -> dict | None:
    """One document in full, including the narrative the map view omits."""
    rows = lakebase.run_query(
        f"""
        SELECT id, location, latitude, longitude, source_type, event, headline,
               narrative_text, severity, area_desc, issued_at, effective_at,
               expires_at, synced_at, payload -> 'geometry' AS geometry
        FROM {documents_table}
        WHERE id = %(id)s
        """,
        {"id": doc_id},
    )
    if not rows:
        return None
    doc = dict(rows[0])
    doc["geometry"] = thin_geometry(doc.get("geometry"))
    return doc
