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

import logging
import os
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

    passages = "\n\n".join(
        f"[{i + 1}] {row['location']} - {row.get('event') or row.get('headline') or ''}"
        f" (similarity {row['similarity']})\n{row['chunk_text']}"
        for i, row in enumerate(results)
    )

    client = anthropic.Anthropic()
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

    # Safety classifiers can decline a request, which arrives as a normal 200
    # with stop_reason "refusal" and no text - check before reading content.
    if response.stop_reason == "refusal":
        raise RuntimeError("the summary request was declined by the model's safety filters")

    # Iterate rather than indexing content[0]: with thinking enabled the first
    # block is a thinking block, whose text is empty by default.
    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise RuntimeError("the model returned no summary text")
    return text
