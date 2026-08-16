"""
In-app refresh scheduler.

Runs weather_refresh.refresh_once() on a timer inside the Flask process, so the
corpus stays current without an external scheduler.

Why here rather than a Databricks Job: this workspace is serverless-only, and a
serverless job task that loads `requests`, `psycopg2` and `fastembed` into one
kernel segfaults it (each pair alone is fine - see DEPLOY.md for the evidence).
The app process already runs exactly that combination successfully, because it
has to in order to serve /weather/search. So the reliable place to run the
refresh is the process that already works.

Safety properties this leans on:

  * Every step of a cycle is idempotent, so a second replica running its own
    timer duplicates work but cannot corrupt anything.
  * A lock prevents a slow cycle from overlapping the next tick.
  * The thread is a daemon and swallows its exceptions, so a failing refresh
    degrades freshness without ever taking the web app down.

Disable by setting WEATHER_REFRESH_MINUTES=0.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger("weather-app.scheduler")

REFRESH_MINUTES = int(os.environ.get("WEATHER_REFRESH_MINUTES", "30"))
# Wait before the first cycle so app startup - and its health check - is never
# competing with a model load and a few dozen outbound HTTPS calls.
STARTUP_DELAY_SECONDS = int(os.environ.get("WEATHER_REFRESH_STARTUP_DELAY", "60"))
REFRESH_LIMIT = int(os.environ.get("WEATHER_REFRESH_LIMIT", "50"))
PURGE_DAYS = int(os.environ.get("WEATHER_PURGE_EXPIRED_DAYS", "7"))

_lock = threading.Lock()
_thread: threading.Thread | None = None
_state: dict = {
    "enabled": REFRESH_MINUTES > 0,
    "interval_minutes": REFRESH_MINUTES,
    "cycles": 0,
    "failures": 0,
    "last_started_at": None,
    "last_finished_at": None,
    "last_result": None,
    "last_error": None,
    "running": False,
}


def status() -> dict:
    """Snapshot of scheduler state, for GET /weather/refresh/status."""
    return dict(_state)


def run_once(locations=None, limit: int | None = None) -> dict:
    """Run a single cycle under the lock. Used by the timer and the manual route.

    Returns a dict with "skipped": True when another cycle holds the lock -
    the caller gets an immediate, honest answer rather than blocking behind it.
    """
    import weather_refresh

    if not _lock.acquire(blocking=False):
        return {"skipped": True, "reason": "a refresh is already running"}
    try:
        _state["running"] = True
        _state["last_started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        result = weather_refresh.refresh_once(
            locations=locations,
            limit=limit if limit is not None else REFRESH_LIMIT,
            purge_expired_days=PURGE_DAYS,
            log=logger.info,
        )
        _state["cycles"] += 1
        _state["last_result"] = result
        _state["last_error"] = None
        return result
    except Exception as err:
        _state["failures"] += 1
        _state["last_error"] = f"{type(err).__name__}: {err}"
        logger.exception("refresh cycle failed")
        raise
    finally:
        _state["running"] = False
        _state["last_finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _lock.release()


def _loop():
    time.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            result = run_once()
            if result.get("skipped"):
                logger.info("refresh skipped: %s", result.get("reason"))
            else:
                logger.info(
                    "refresh ok: fetched=%s upserted=%s invalidated=%s purged=%s "
                    "embedded=%s in %ss",
                    result["fetched"], result["upserted"],
                    result["embeddings_invalidated"], result["purged"],
                    result["embedded_written"], result["elapsed_seconds"],
                )
        except Exception:
            # Already logged with a traceback in run_once. Swallowed on purpose:
            # a failed cycle must not kill the timer, or one bad refresh would
            # silently stop every future one.
            pass
        time.sleep(max(60, REFRESH_MINUTES * 60))


def start() -> bool:
    """Start the background timer. Idempotent; returns whether it is running."""
    global _thread

    if REFRESH_MINUTES <= 0:
        logger.info("scheduler disabled (WEATHER_REFRESH_MINUTES=0)")
        return False
    if _thread is not None and _thread.is_alive():
        return True

    _thread = threading.Thread(target=_loop, name="weather-refresh", daemon=True)
    _thread.start()
    logger.info(
        "scheduler started: every %s min, first cycle in %ss",
        REFRESH_MINUTES, STARTUP_DELAY_SECONDS,
    )
    return True
