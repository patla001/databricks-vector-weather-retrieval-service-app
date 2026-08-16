"""
One refresh cycle: harvest -> upsert -> purge -> embed.

Shared by three callers so the behaviour can't drift between them:

  * the in-app scheduler (weather_scheduler.py), which is how the deployed app
    keeps itself current
  * notebooks/scheduled_weather_refresh.py, for a manual or externally
    scheduled run
  * anything else that wants a single "make the corpus current" call

Every step is idempotent. Documents upsert on their natural id, chunks collide
on a derived primary key, and the purge only removes alerts that expired days
ago - so running two cycles concurrently is wasteful but never corrupting.
That property is what makes it safe for the app to run this on a timer even if
the platform ever runs more than one replica.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable

import weather_pipeline
from weather_client import VALID_SOURCES, WeatherClient

DEFAULT_LOCATIONS = ["Chicago, IL", "Austin, TX", "Houston, TX", "Miami, FL", "Denver, CO"]


def purge_expired(days: int) -> int:
    """Delete alerts whose expiry is more than `days` old.

    Without this the corpus only grows: an alert that expired last month still
    matches queries and dilutes results with weather that is no longer true.
    Forecast periods are left alone - they are keyed by period start and get
    replaced by the next harvest rather than accumulating.

    The FK's ON DELETE CASCADE takes the matching embedding rows with it, so
    there is no second statement and no window in which orphans exist.
    """
    import lakebase

    return lakebase.run_write(
        f"""
        DELETE FROM {weather_pipeline.DEFAULT_DOCUMENTS_TABLE}
        WHERE source_type = 'alert'
          AND expires_at IS NOT NULL
          AND expires_at < now() - make_interval(days => %s)
        """,
        (days,),
    )


def refresh_once(
    locations: Iterable[str] | None = None,
    limit: int = 50,
    sources: Iterable[str] = VALID_SOURCES,
    purge_expired_days: int = 7,
    embed: bool = True,
    log: Callable[[str], None] = print,
) -> dict:
    """Run one full cycle and return a summary dict.

    Never raises for an upstream failure: NWS returning 500 for one location is
    an expected condition, not a reason to abandon the other four. Those
    failures come back in the "errors" key so a caller can surface them.
    """
    started = time.time()
    locations = list(locations) if locations else list(DEFAULT_LOCATIONS)
    sources = list(sources)

    client = WeatherClient()
    documents, errors = client.fetch_documents(
        locations, limit=limit, sources=sources, log=log
    )

    upserted = weather_pipeline.upsert_documents(documents)

    purged = 0
    if purge_expired_days > 0:
        purged = purge_expired(purge_expired_days)

    embedded = {"documents": 0, "chunks": 0, "written": 0}
    if embed:
        import weather_search  # deferred: pulls in the ONNX runtime

        pending = weather_pipeline.pending_documents()
        if pending:
            embedded = weather_pipeline.embed_documents(
                weather_search.embed_texts,
                weather_search.EMBED_MODEL,
                pending,
                log=log,
            )

    stats = weather_pipeline.summarize()
    return {
        "locations": locations,
        "fetched": len(documents),
        "upserted": upserted["written"],
        "embeddings_invalidated": upserted["reembed"],
        "purged": purged,
        "embedded_documents": embedded["documents"],
        "embedded_chunks": embedded["chunks"],
        "embedded_written": embedded["written"],
        "stats": stats,
        "errors": errors,
        "elapsed_seconds": round(time.time() - started, 2),
    }
