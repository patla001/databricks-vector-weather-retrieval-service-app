"""
Weather intelligence REST API.

Harvests free-text weather from the National Weather Service into Lakebase
(Postgres + pgvector), and serves semantic search over it:

    POST /weather/sync    {"locations": ["Chicago, IL"], "limit": 50}
    POST /weather/search  {"query": "flash flood risk this weekend", "top_k": 5}
    GET  /weather/search?query=...&summarize=true

load_dotenv() runs before `import lakebase` on purpose: those modules read
os.environ at import time, so the .env overrides have to exist first.
"""

import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

load_dotenv()

import lakebase  # noqa: E402
import weather_pipeline  # noqa: E402
from weather_client import (  # noqa: E402
    VALID_SOURCES,
    LocationError,
    WeatherClient,
    resolve_location,
    supported_cities,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-app")

app = Flask(__name__)

DOCUMENTS_TABLE = weather_pipeline.DEFAULT_DOCUMENTS_TABLE
EMBEDDINGS_TABLE = weather_pipeline.DEFAULT_EMBEDDINGS_TABLE

# Pipe-separated, not comma-separated: a location is itself "City, ST", so a
# comma-delimited list would split "Chicago, IL" into two useless fragments.
DEFAULT_LOCATIONS = [
    loc.strip()
    for loc in os.environ.get(
        "WEATHER_LOCATIONS", "Chicago, IL|Austin, TX|Houston, TX|Miami, FL|Denver, CO"
    ).split("|")
    if loc.strip()
]

MAX_LOCATIONS_PER_SYNC = 25
MAX_SYNC_LIMIT = 500


def ensure_weather_documents_table():
    """Create the raw weather-documents table if it doesn't exist yet.

    Mirrors sql/01_setup_weather_documents.sql so a fresh deploy self-heals.
    The embeddings table is deliberately NOT created here: CREATE EXTENSION and
    the HNSW index are privileged one-time DDL that a least-privilege app role
    generally can't run, so sql/02 stays a manual step.
    """
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {DOCUMENTS_TABLE} (
            id             TEXT PRIMARY KEY,
            location       TEXT NOT NULL,
            latitude       DOUBLE PRECISION,
            longitude      DOUBLE PRECISION,
            source_type    TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),
            event          TEXT,
            headline       TEXT,
            narrative_text TEXT NOT NULL,
            text_hash      TEXT NOT NULL,
            severity       TEXT,
            area_desc      TEXT,
            issued_at      TIMESTAMPTZ,
            effective_at   TIMESTAMPTZ,
            expires_at     TIMESTAMPTZ,
            payload        JSONB NOT NULL,
            synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    for column in ("location", "source_type"):
        lakebase.run_write(
            f"CREATE INDEX IF NOT EXISTS idx_{DOCUMENTS_TABLE}_{column} "
            f"ON {DOCUMENTS_TABLE} ({column})"
        )


def _embeddings_table_exists() -> bool:
    """Whether the pgvector table has been created (sql/02 has been run)."""
    rows = lakebase.run_query(
        "SELECT to_regclass(%s) IS NOT NULL AS present", (EMBEDDINGS_TABLE,)
    )
    return bool(rows and rows[0]["present"])


def _bad_request(message: str):
    return jsonify({"error": message}), 400


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON, not an HTML error page.

    The response deliberately does NOT include str(err). Exception text
    routinely carries credentials: a psycopg2 connection failure puts the entire
    DSN, password and all, into its message. Full detail goes to the logs.

    HTTPExceptions are the exception - their descriptions are authored by Flask
    or by us, so they carry no internals. Routes that reject bad input do so
    with an explicit `return jsonify(...), 400` and never reach this handler.
    """
    if isinstance(err, HTTPException):
        logger.info("HTTP %s on %s: %s", err.code, request.path, err.description)
        return jsonify({"error": err.description}), err.code

    logger.exception("Unhandled exception while processing request")
    return jsonify({"error": "Internal server error"}), 500


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    """Point a browser at the app and get the API surface, not a 404."""
    return jsonify(
        {
            "service": "weather-intelligence",
            "endpoints": {
                "GET  /healthz": "liveness probe",
                "POST /weather/sync": 'body: {"locations": ["Chicago, IL"], "limit": 50, '
                                      '"sources": ["alert", "forecast"]}',
                "POST /weather/search": 'body: {"query": "...", "top_k": 5, '
                                        '"source_type": "alert"}',
                "GET  /weather/search": "?query=...&top_k=5&source_type=alert&summarize=true",
            },
            "supported_locations": supported_cities(),
            "coordinate_form": '"lat,lon" e.g. "41.88,-87.63"',
        }
    )


# ---------------------------------------------------------------------------
# Part 1 - harvest
# ---------------------------------------------------------------------------


@app.route("/weather/sync", methods=["POST"])
def sync_weather():
    """
    Fetch active alerts and narrative forecasts from api.weather.gov for a set
    of locations and upsert them into weather_documents.

    Body (all optional): {
      "locations": ["Chicago, IL", "29.76,-95.37"],
      "limit": 50,                       # per location, per source
      "sources": ["alert", "forecast"]
    }

    Re-running is safe: documents upsert on their natural id, so the row count
    stays flat rather than growing. `limit` is applied client-side because
    api.weather.gov rejects a limit parameter on /alerts/active with a 400.
    """
    body = request.json if request.is_json else {}
    if not isinstance(body, dict):
        return _bad_request("Request body must be a JSON object.")

    locations = body.get("locations") or DEFAULT_LOCATIONS
    if not isinstance(locations, list):
        return _bad_request('"locations" must be a list of strings.')
    locations = [loc.strip() for loc in locations if isinstance(loc, str) and loc.strip()]
    if not locations:
        return _bad_request('"locations" must contain at least one non-empty string.')
    if len(locations) > MAX_LOCATIONS_PER_SYNC:
        return _bad_request(
            f"Too many locations ({len(locations)}); the maximum is {MAX_LOCATIONS_PER_SYNC}. "
            "Each location costs at least two upstream API calls."
        )

    try:
        limit = int(body.get("limit", 50))
    except (TypeError, ValueError):
        return _bad_request('"limit" must be an integer.')
    limit = max(1, min(limit, MAX_SYNC_LIMIT))

    sources = body.get("sources") or list(VALID_SOURCES)
    if not isinstance(sources, list):
        return _bad_request('"sources" must be a list.')
    invalid = [s for s in sources if s not in VALID_SOURCES]
    if invalid:
        return _bad_request(
            f"Invalid source type(s): {invalid}. Valid values are {list(VALID_SOURCES)}."
        )

    # Resolve every location before any network work, so a typo fails fast with
    # a useful message instead of half-syncing and then erroring partway through.
    try:
        for location in locations:
            resolve_location(location)
    except LocationError as err:
        return _bad_request(str(err))

    ensure_weather_documents_table()

    client = WeatherClient()
    documents, errors = client.fetch_documents(
        locations, limit=limit, sources=sources, log=logger.info
    )
    result = weather_pipeline.upsert_documents(documents)

    by_source: dict[str, int] = {}
    for doc in documents:
        by_source[doc["source_type"]] = by_source.get(doc["source_type"], 0) + 1

    payload = {
        "synced": result["written"],
        "locations": locations,
        "by_source": by_source,
        "embeddings_invalidated": result["reembed"],
    }
    if errors:
        # Report partial failure rather than implying the count covers
        # everything that was asked for.
        payload["errors"] = errors
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Part 3 - retrieve
# ---------------------------------------------------------------------------


def _run_search(query, top_k, source_type, location, summarize):
    """Shared body of the POST and GET search routes."""
    import weather_search  # imported lazily: pulls in the ONNX runtime

    if not _embeddings_table_exists():
        return (
            jsonify(
                {
                    "error": f"The {EMBEDDINGS_TABLE} table does not exist. Run "
                    "sql/02_setup_weather_embeddings.sql in the Lakebase SQL editor, "
                    "then run notebooks/ingest_weather_embeddings.py."
                }
            ),
            409,
        )

    top_k = weather_search.clamp_top_k(top_k)
    results = weather_search.search_weather(
        query, top_k=top_k, source_type=source_type, location=location
    )

    payload = {
        "query": query,
        "top_k": top_k,
        "source_type": source_type,
        "location": location,
        "count": len(results),
        "results": results,
    }

    # An empty corpus is a normal state for a fresh deployment, not an error -
    # 200 with an empty list, plus a hint about what to run next.
    if not results:
        payload["note"] = (
            "No matches. If nothing has been ingested yet, run POST /weather/sync "
            "and then notebooks/ingest_weather_embeddings.py."
        )

    if summarize:
        try:
            payload["summary"] = weather_search.summarize_results(query, results)
        except Exception as err:
            # The summary is a stretch feature; never let it fail the search.
            logger.warning("summary unavailable: %s", err)
            payload["summary_error"] = str(err)

    return jsonify(payload)


@app.route("/weather/search", methods=["POST"])
def search_weather_post():
    """
    Semantic search over the embedded weather corpus.

    Body: {"query": "risk of flooding near rivers", "top_k": 5,
           "source_type": "alert", "location": "Chicago, IL"}

    top_k is clamped to 1-20. source_type filters to alerts or forecasts.
    """
    body = request.json if request.is_json else {}
    if not isinstance(body, dict):
        return _bad_request("Request body must be a JSON object.")

    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return _bad_request("Missing required field: query (a non-empty string).")
    query = query.strip()

    raw_top_k = body.get("top_k", 5)
    if isinstance(raw_top_k, bool) or not isinstance(raw_top_k, (int, float, str)):
        return _bad_request('"top_k" must be an integer.')
    try:
        top_k = int(raw_top_k)
    except (TypeError, ValueError):
        return _bad_request('"top_k" must be an integer.')

    source_type = body.get("source_type")
    if source_type is not None:
        if not isinstance(source_type, str) or source_type not in VALID_SOURCES:
            return _bad_request(
                f"source_type must be one of {list(VALID_SOURCES)}, got {source_type!r}."
            )

    location = body.get("location")
    if location is not None and (not isinstance(location, str) or not location.strip()):
        return _bad_request('"location" must be a non-empty string when provided.')
    location = location.strip() if isinstance(location, str) else None

    return _run_search(query, top_k, source_type, location, bool(body.get("summarize")))


@app.route("/weather/search", methods=["GET"])
def search_weather_get():
    """
    Browser-friendly variant of POST /weather/search.

        GET /weather/search?query=severe+thunderstorm&top_k=5&summarize=true

    With summarize=true the response also carries an LLM-written paragraph
    grounded in the retrieved chunks (basic RAG). If no API key is configured
    the results still come back, with a "summary_error" note beside them.
    """
    query = (request.args.get("query") or request.args.get("q") or "").strip()
    if not query:
        return _bad_request("Missing required query parameter: query")

    try:
        top_k = int(request.args.get("top_k", 5))
    except ValueError:
        return _bad_request("top_k must be an integer.")

    source_type = request.args.get("source_type") or None
    if source_type is not None and source_type not in VALID_SOURCES:
        return _bad_request(
            f"source_type must be one of {list(VALID_SOURCES)}, got {source_type!r}."
        )

    location = (request.args.get("location") or "").strip() or None
    summarize = (request.args.get("summarize") or "").lower() in ("1", "true", "yes")

    return _run_search(query, top_k, source_type, location, summarize)


@app.route("/weather/embed", methods=["POST"])
def embed_pending():
    """
    Embed documents that have no vectors yet.

    Body (optional): {"limit": 200}

    This exists so the scheduled refresh job doesn't need its own copy of the
    embedding stack. The app already holds the model warm for /weather/search,
    so the job can stay a thin HTTP client with no ONNX runtime, no psycopg2,
    and no chance of the two sides drifting onto different model exports.

    Idempotent: chunks collide on their derived primary key and are skipped, so
    calling it repeatedly converges rather than duplicating.
    """
    import weather_search  # lazy: pulls in the ONNX runtime on first use

    if not _embeddings_table_exists():
        return (
            jsonify({"error": f"The {EMBEDDINGS_TABLE} table does not exist. Run "
                              "sql/02_setup_weather_embeddings.sql first."}),
            409,
        )

    body = request.json if request.is_json else {}
    if not isinstance(body, dict):
        return _bad_request("Request body must be a JSON object.")

    limit = body.get("limit")
    if limit is not None:
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            return _bad_request('"limit" must be an integer.')

    pending = weather_pipeline.pending_documents(limit=limit)
    if not pending:
        return jsonify({"pending": 0, "documents": 0, "chunks": 0, "written": 0,
                        "note": "nothing to embed"})

    result = weather_pipeline.embed_documents(
        weather_search.embed_texts,
        weather_search.EMBED_MODEL,
        pending,
        log=logger.info,
    )
    remaining = weather_pipeline.summarize()["pending"]
    return jsonify({**result, "model": weather_search.EMBED_MODEL, "remaining": remaining})


@app.route("/weather/stats")
def weather_stats():
    """Row counts for both tables plus the unembedded backlog."""
    if not _embeddings_table_exists():
        return jsonify({"error": f"The {EMBEDDINGS_TABLE} table does not exist."}), 409
    return jsonify(weather_pipeline.summarize())


@app.route("/weather/refresh/status")
def refresh_status():
    """What the in-app scheduler has been doing.

    Active NWS alerts expire within hours, so "is the refresh loop alive?" is
    the difference between a live corpus and one that quietly goes stale. This
    makes that answerable without reading container logs.
    """
    import weather_scheduler

    return jsonify(weather_scheduler.status())


@app.route("/weather/refresh", methods=["POST"])
def refresh_now():
    """Run one refresh cycle immediately (harvest + purge + embed).

    Body (optional): {"locations": [...], "limit": 50}

    The scheduler runs this on a timer; this route is for forcing a cycle after
    a deploy or while testing. Returns 409 rather than queueing if a cycle is
    already in flight.
    """
    import weather_scheduler

    body = request.json if request.is_json else {}
    if not isinstance(body, dict):
        return _bad_request("Request body must be a JSON object.")

    locations = body.get("locations")
    if locations is not None:
        if not isinstance(locations, list) or not all(isinstance(x, str) for x in locations):
            return _bad_request('"locations" must be a list of strings.')
        try:
            for location in locations:
                resolve_location(location)
        except LocationError as err:
            return _bad_request(str(err))

    limit = body.get("limit")
    if limit is not None:
        try:
            limit = max(1, min(int(limit), MAX_SYNC_LIMIT))
        except (TypeError, ValueError):
            return _bad_request('"limit" must be an integer.')

    ensure_weather_documents_table()
    result = weather_scheduler.run_once(locations=locations, limit=limit)
    if result.get("skipped"):
        return jsonify(result), 409
    return jsonify(result)


def _start_scheduler():
    """Start the background refresh timer, once, in the serving process.

    Guarded against Flask's reloader, which runs the module twice in debug mode
    and would otherwise give you two timers racing each other.
    """
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        try:
            import weather_scheduler

            weather_scheduler.start()
        except Exception:
            # A scheduler that won't start must not stop the API from serving.
            logger.exception("could not start the refresh scheduler")


# Started at import so it runs under a WSGI server (which never executes the
# __main__ block below), not just under `python app.py`.
_start_scheduler()


if __name__ == '__main__':
    host = os.getenv('FLASK_RUN_HOST', '0.0.0.0')
    # DATABRICKS_APP_PORT must win - the platform health-checks that exact port.
    port = int(os.getenv('DATABRICKS_APP_PORT') or os.getenv('FLASK_RUN_PORT') or 8000)
    debug = os.getenv('FLASK_DEBUG', '1') == '1'
    logger.info("Starting Flask on http://%s:%s (debug=%s)", host, port, debug)
    app.run(debug=debug, host=host, port=port)
