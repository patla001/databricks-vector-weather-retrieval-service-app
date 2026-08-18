"""
Semantic search over the weather embeddings in Lakebase.

Query vectors MUST come from the same model that produced the stored vectors
(all-MiniLM-L6-v2, 384 dims) - a different model puts the query in a different
space and the cosine distances become meaningless. That is why both this module
and the ingestion job import `embed_texts` from here rather than each building
their own embedder.

The model is served through `fastembed`, which runs the ONNX export on CPU,
rather than through `sentence-transformers`, which pulls in ~2.5GB of torch.
The two are the same weights and agree to cosine 0.99+, so rankings are
equivalent while the app stays small enough to cold-start inside a Databricks
App container.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from functools import lru_cache
from typing import Sequence

import lakebase
from weather_pipeline import (
    DEFAULT_DOCUMENTS_TABLE,
    DEFAULT_EMBEDDINGS_TABLE,
    thin_geometry,
    to_vector_literal,
)

logger = logging.getLogger(__name__)

EMBED_MODEL = os.environ.get(
    "WEATHER_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
DOCUMENTS_TABLE = DEFAULT_DOCUMENTS_TABLE
EMBEDDINGS_TABLE = DEFAULT_EMBEDDINGS_TABLE

# The model is downloaded on first use; point it somewhere writable. Databricks
# Apps and most containers only guarantee /tmp.
os.environ.setdefault("FASTEMBED_CACHE_PATH", "/tmp/.cache/fastembed")

# top_k bounds. Clamped rather than rejected so a stray top_k=100000 can't pull
# the whole corpus through the API, and a sensible request never 400s.
MIN_TOP_K = 1
MAX_TOP_K = 20
DEFAULT_TOP_K = 5

VALID_SOURCE_TYPES = ("alert", "forecast")


@lru_cache(maxsize=1)
def _embedder():
    """Load the ONNX model once, on first use rather than at import.

    lru_cache is what makes "load the model once, not per request" true: app.py
    imports this module lazily inside the route, and every subsequent search
    reuses this instance. Keeps startup fast, and an app that never searches
    never pays the download.
    """
    from fastembed import TextEmbedding

    logger.info("loading embedding model %s", EMBED_MODEL)
    return TextEmbedding(EMBED_MODEL)


def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Embed a batch of strings. Shared with the ingestion job."""
    return [vector.tolist() for vector in _embedder().embed(list(texts))]


def embed_query(text: str) -> list[float]:
    """Embed a search query into the same space as the stored vectors."""
    return embed_texts([text])[0]


def clamp_top_k(value: int) -> int:
    return max(MIN_TOP_K, min(int(value), MAX_TOP_K))


def search_weather(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    source_type: str | None = None,
    location: str | None = None,
) -> list[dict]:
    """Nearest weather passages to `query`, by cosine similarity.

    Returns [] on an empty corpus rather than raising - "nothing synced yet" is
    a normal state for a fresh deployment, not an error.

    The two optional filters are applied with the
    `%(x)s::text IS NULL OR col = %(x)s` idiom so one query serves the filtered
    and unfiltered cases; source_type is matched on the embeddings table, where
    it is denormalized, so the planner can discard rows before the join.
    """
    vector = to_vector_literal(embed_query(query))

    rows = lakebase.run_query(
        f"""
        SELECT d.id,
               d.location,
               d.latitude,
               d.longitude,
               d.source_type,
               d.event,
               d.headline,
               d.narrative_text,
               d.severity,
               d.area_desc,
               d.issued_at,
               d.expires_at,
               e.chunk_index,
               e.chunk_text,
               -- The map draws the alert's real footprint where NWS published one.
               -- Roughly a quarter of alerts are zone-based and carry no polygon;
               -- those come back null and the UI falls back to the city point.
               d.payload -> 'geometry' AS geometry,
               -- ::float8 is load-bearing. numeric arrives as a Python Decimal,
               -- which Flask serializes as a JSON *string* - so every consumer
               -- would get "0.7051" and any arithmetic on it would fail.
               ROUND((1 - (e.embedding <=> %(vec)s::vector))::numeric, 4)::float8 AS similarity
        FROM {EMBEDDINGS_TABLE} e
        JOIN {DOCUMENTS_TABLE} d ON d.id = e.document_id
        WHERE (%(source_type)s::text IS NULL OR e.source_type = %(source_type)s)
          AND (%(location)s::text    IS NULL OR d.location    = %(location)s)
        ORDER BY e.embedding <=> %(vec)s::vector
        LIMIT %(top_k)s
        """,
        {
            "vec": vector,
            "source_type": source_type,
            "location": location,
            "top_k": clamp_top_k(top_k),
        },
    )

    # Thin the polygons through the same function the map endpoint uses. The two
    # responses feed the same globe, so a raw GeoJSON `coordinates` here and a
    # thinned `rings` there is a shape mismatch waiting to break the caller.
    for row in rows:
        row["geometry"] = thin_geometry(row.get("geometry"))
    return rows


# ---------------------------------------------------------------------------
# Optional RAG summary
# ---------------------------------------------------------------------------

SUMMARY_MODEL = os.environ.get("WEATHER_SUMMARY_MODEL", "claude-opus-5")

# ---------------------------------------------------------------------------
# Summary guardrails
#
# The summary is the only thing in this app that spends money per request:
# everything else - sync, embed, map, the refresh scheduler - talks to NWS and
# Postgres and costs nothing per call. So the bounds live here rather than as a
# generic middleware, and each one degrades to search-only rather than failing
# the request.
# ---------------------------------------------------------------------------

# Wall-clock bound on the API call. The SDK's default is 10 minutes, which on a
# small container means a handful of stuck calls hold every worker and the whole
# app stops answering - a availability failure long before it is a cost one.
SUMMARY_TIMEOUT = float(os.environ.get("WEATHER_SUMMARY_TIMEOUT", "60"))
SUMMARY_MAX_RETRIES = int(os.environ.get("WEATHER_SUMMARY_MAX_RETRIES", "2"))

# Calls per UTC day, across the whole app. This is the backstop for a runaway
# loop; the real ceiling should also be set on the Anthropic Console, because a
# limit enforced inside the process cannot help if the process is the problem.
SUMMARY_DAILY_LIMIT = int(os.environ.get("WEATHER_SUMMARY_DAILY_LIMIT", "200"))

SUMMARY_CACHE_SIZE = int(os.environ.get("WEATHER_SUMMARY_CACHE_SIZE", "256"))
SUMMARY_CACHE_TTL = float(os.environ.get("WEATHER_SUMMARY_CACHE_TTL", "3600"))

_summary_lock = threading.Lock()
_summary_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_summary_usage = {"date": "", "calls": 0, "cache_hits": 0, "throttled": 0}


def _summary_cache_key(query: str, results: list[dict]) -> str:
    """Identify a summary by its question and the exact evidence behind it.

    Keying on the retrieved chunks rather than on the query alone is what makes
    the cache safe against a moving corpus: when the refresh cycle changes which
    passages a query retrieves, the key changes with it and the stale summary is
    never served. The model id is in the key for the same reason - switching
    WEATHER_SUMMARY_MODEL must not serve answers written by the previous one.
    """
    evidence = "|".join(f"{row['id']}:{row['chunk_index']}" for row in results)
    raw = f"{SUMMARY_MODEL}\x00{query}\x00{evidence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> str | None:
    now = time.time()
    with _summary_lock:
        entry = _summary_cache.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if now - stored_at > SUMMARY_CACHE_TTL:
            del _summary_cache[key]
            return None
        _summary_cache.move_to_end(key)
        _summary_usage["cache_hits"] += 1
        return value


def _cache_put(key: str, value: str) -> None:
    with _summary_lock:
        _summary_cache[key] = (time.time(), value)
        _summary_cache.move_to_end(key)
        while len(_summary_cache) > SUMMARY_CACHE_SIZE:
            _summary_cache.popitem(last=False)


def _claim_budget() -> None:
    """Reserve one call against today's ceiling, or refuse.

    Claimed before the request rather than counted after it, so concurrent
    callers cannot both pass the check and then both spend.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    with _summary_lock:
        if _summary_usage["date"] != today:
            _summary_usage.update(date=today, calls=0, cache_hits=0, throttled=0)
        if SUMMARY_DAILY_LIMIT and _summary_usage["calls"] >= SUMMARY_DAILY_LIMIT:
            _summary_usage["throttled"] += 1
            raise RuntimeError(
                f"the daily summary limit of {SUMMARY_DAILY_LIMIT} has been reached; "
                f"search results are unaffected and the limit resets at 00:00 UTC"
            )
        _summary_usage["calls"] += 1


def _release_budget() -> None:
    """Hand a claim back when the call never reached the model.

    A timeout or a transport error costs nothing, so charging it against the
    day's ceiling would let a broken upstream silently exhaust the budget.
    """
    with _summary_lock:
        _summary_usage["calls"] = max(0, _summary_usage["calls"] - 1)


def summary_status() -> dict:
    """What the summary path has spent today. Cheap enough to poll."""
    today = datetime.now(timezone.utc).date().isoformat()
    with _summary_lock:
        calls = _summary_usage["calls"] if _summary_usage["date"] == today else 0
        hits = _summary_usage["cache_hits"] if _summary_usage["date"] == today else 0
        throttled = _summary_usage["throttled"] if _summary_usage["date"] == today else 0
        cached = len(_summary_cache)
    return {
        "model": SUMMARY_MODEL,
        "enabled": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "calls_today": calls,
        "daily_limit": SUMMARY_DAILY_LIMIT,
        "remaining_today": max(0, SUMMARY_DAILY_LIMIT - calls) if SUMMARY_DAILY_LIMIT else None,
        "cache_hits_today": hits,
        "throttled_today": throttled,
        "cached_summaries": cached,
    }

_SUMMARY_SYSTEM = (
    "You summarize National Weather Service text for someone asking about "
    "conditions. Answer only from the passages provided. If they do not cover "
    "the question, say so plainly rather than filling the gap. Name the "
    "locations and hazards you are drawing on, keep it to a short paragraph, "
    "and never invent a time, place, or severity that is not in the passages."
)


def summarize_results(query: str, results: list[dict]) -> str:
    """One-paragraph natural-language answer grounded in the retrieved chunks.

    Raises RuntimeError when the Anthropic SDK or ANTHROPIC_API_KEY is missing,
    so the caller can report that as a note beside the results instead of
    failing the whole search - retrieval is the graded feature here, and the
    summary is an extra that must never take it down.
    """
    if not results:
        return "No matching weather documents were found, so there is nothing to summarize."

    try:
        import anthropic
    except ImportError as err:  # pragma: no cover - depends on the install
        raise RuntimeError("the `anthropic` package is not installed") from err

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    # Serve an identical question over identical evidence for free. In practice
    # this is the highest-value guardrail here: the same handful of queries get
    # run over and over while the corpus barely moves between refreshes.
    cache_key = _summary_cache_key(query, results)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    passages = "\n\n".join(
        f"[{i + 1}] {row['location']} - {row.get('event') or row.get('headline') or ''}"
        f" (similarity {row['similarity']})\n{row['chunk_text']}"
        for i, row in enumerate(results)
    )

    # A cache miss is the only path that spends money, so the ceiling is claimed
    # here rather than at the top of the function.
    _claim_budget()

    client = anthropic.Anthropic(
        timeout=SUMMARY_TIMEOUT, max_retries=SUMMARY_MAX_RETRIES
    )
    try:
        response = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=1024,
            # Thinking is on by default on this model. A grounded one-paragraph
            # summary over passages that are already retrieved needs very little
            # deliberation, so low effort keeps the endpoint responsive - and is
            # the recommended lever over disabling thinking outright.
            output_config={"effort": "low"},
            system=_SUMMARY_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"Question: {query}\n\nWeather passages:\n\n{passages}",
                }
            ],
        )
    except Exception:
        # Nothing was generated, so nothing should be charged against the day.
        _release_budget()
        raise

    # Safety classifiers can decline a request, which arrives as a normal 200
    # with stop_reason "refusal" and no text - check before reading content.
    # Deliberately outside the try above, so a refusal keeps its budget claim.
    # A pre-output decline is not billed by the API, but a client retrying a
    # refused query in a loop should still run into the daily ceiling.
    if response.stop_reason == "refusal":
        raise RuntimeError("the summary request was declined by the model's safety filters")

    # Iterate rather than indexing content[0]: with thinking enabled the first
    # block is a thinking block, whose text is empty by default.
    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise RuntimeError("the model returned no summary text")

    _cache_put(cache_key, text)
    return text
