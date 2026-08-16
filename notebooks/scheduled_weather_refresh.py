"""
Scheduled refresh: harvest -> upsert -> embed, in one run.

This is what keeps the corpus current. Active NWS alerts expire within hours, so
a corpus that is only populated once goes quiet by the next day. Run this on a
schedule (see resources/weather_refresh_job.yml) and search stays live.

    python notebooks/scheduled_weather_refresh.py
    python notebooks/scheduled_weather_refresh.py --locations "Chicago, IL|Miami, FL"
    python notebooks/scheduled_weather_refresh.py --purge-expired-days 7

It talks to Lakebase directly rather than going through the Flask app: the job
needs no app URL, no OAuth round trip, and no dependency on the app being up.
The app and this job are two clients of the same database.

Credentials resolve the same way everywhere. LAKEBASE_URL wins when set (local
runs, off .env); otherwise lakebase.py reads the Databricks secret scope, which
is the path a Databricks Job takes. The secret KEY is defaulted to
`weather-lakebase-url` below, because lakebase.py's own default is the day-2
app's `lakebase-url` and the two must not be confused.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

def _repo_root() -> str:
    """Locate the repo root so `import weather_pipeline` works anywhere.

    Databricks serverless runs a spark_python_task through
    `exec(compile(f.read(), filename, 'exec'))`, which leaves `__file__`
    UNDEFINED - so the obvious os.path.dirname(__file__) raises NameError there
    while working fine locally. Hence the ladder:

      1. WEATHER_REPO_ROOT, when the caller knows and says so
      2. __file__, for a normal `python notebooks/...` run
      3. a walk up from cwd looking for the package's own marker file
    """
    env_root = os.environ.get("WEATHER_REPO_ROOT")
    if env_root and os.path.isfile(os.path.join(env_root, "weather_pipeline.py")):
        return env_root

    # Also honour --repo-root from argv. sys.path has to be set up before
    # argparse runs, so this is read directly rather than through the parser.
    if "--repo-root" in sys.argv:
        candidate = sys.argv[sys.argv.index("--repo-root") + 1]
        if os.path.isfile(os.path.join(candidate, "weather_pipeline.py")):
            return candidate

    try:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass

    here = os.getcwd()
    for _ in range(6):
        if os.path.isfile(os.path.join(here, "weather_pipeline.py")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return os.getcwd()


_ROOT = _repo_root()
for path in (_ROOT, os.path.join(_ROOT, "notebooks")):
    if path not in sys.path:
        sys.path.insert(0, path)

# Must be set BEFORE importing lakebase: that module reads the scope and key
# into module-level constants at import time.
os.environ.setdefault("LAKEBASE_SECRET_SCOPE", "database")
os.environ.setdefault("LAKEBASE_SECRET_KEY", "weather-lakebase-url")
# fastembed downloads its ONNX export on first use; job containers only
# guarantee /tmp is writable.
os.environ.setdefault("FASTEMBED_CACHE_PATH", "/tmp/.cache/fastembed")

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:  # pragma: no cover - dotenv is absent on a job cluster
    pass

# weather_client needs only `requests`, so it is safe to import up front even in
# app mode. lakebase and weather_pipeline pull in psycopg2 and are imported
# lazily inside the direct path, so a job container driving the app over HTTP
# never needs a database driver at all.
from weather_client import VALID_SOURCES, WeatherClient

DEFAULT_LOCATIONS = "Chicago, IL|Austin, TX|Houston, TX|Miami, FL|Denver, CO"


def refresh_via_app(app_url: str, locations: list[str], limit: int,
                    sources: list[str]) -> int:
    """Drive the refresh through the deployed app's own endpoints.

    This is the mode the Databricks Job uses. Everything heavy - the ONNX
    runtime, psycopg2, the database credential - stays inside the app, so the
    job container needs only `requests` and the SDK for auth. It also means the
    query path and the ingest path provably share one embedding model, because
    there is only one copy of it.

    The direct mode below is the same work done in-process, which is what you
    want locally and in any environment that can reach Lakebase directly.
    """
    import requests

    session = requests.Session()
    try:
        from databricks.sdk import WorkspaceClient

        session.headers.update(WorkspaceClient().config.authenticate())
    except Exception as err:
        print(f"  ! could not attach Databricks auth: {err}")

    app_url = app_url.rstrip("/")

    def call(method: str, path: str, **kw):
        """Request a JSON endpoint, failing loudly on an auth interception.

        A Databricks App sits behind an OAuth proxy that answers an
        unauthenticated request with the LOGIN PAGE and HTTP 200 - not a 401.
        raise_for_status() is therefore happy and the failure only surfaces as a
        confusing JSONDecodeError deep in the parse. Detect it here and say what
        is actually wrong.
        """
        resp = session.request(method, f"{app_url}{path}", **kw)
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "").split(";")[0]
        if ctype != "application/json":
            raise RuntimeError(
                f"{method} {path} returned {ctype or 'no content-type'} rather than JSON "
                f"(HTTP {resp.status_code}). This is almost always the app's OAuth proxy "
                f"serving its login page: the caller's credentials are not valid for the "
                f"app. Grant the running identity access to the app, or use direct mode "
                f"(drop --app-url) so the refresh talks to Lakebase itself."
            )
        return resp.json()

    print(f"sync   -> {app_url}/weather/sync")
    synced = call("POST", "/weather/sync",
                  json={"locations": locations, "limit": limit, "sources": sources},
                  timeout=600)
    print(f"  synced={synced.get('synced')} by_source={synced.get('by_source')} "
          f"invalidated={synced.get('embeddings_invalidated')}")
    for err in synced.get("errors", []):
        print(f"  ! {err}")

    print(f"embed  -> {app_url}/weather/embed")
    embedded = call("POST", "/weather/embed", json={}, timeout=900)
    print(f"  documents={embedded.get('documents')} chunks={embedded.get('chunks')} "
          f"written={embedded.get('written')} remaining={embedded.get('remaining')}")

    stats = call("GET", "/weather/stats", timeout=120)
    print(f"\nafter:  {stats}")

    if stats.get("pending"):
        print(f"\nWARNING: {stats['pending']} document(s) still unembedded")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-url", default=os.environ.get("WEATHER_APP_URL"),
        help="Drive the refresh through the deployed app's HTTP endpoints instead "
             "of connecting to Lakebase from this process. Keeps the job container "
             "free of the embedding stack.",
    )
    parser.add_argument(
        "--locations",
        default=os.environ.get("WEATHER_LOCATIONS", DEFAULT_LOCATIONS),
        help='Pipe-separated, e.g. "Chicago, IL|Miami, FL". Pipe rather than '
             'comma because each entry contains its own comma.',
    )
    parser.add_argument("--limit", type=int, default=50,
                        help="Max documents per location per source (default: 50).")
    parser.add_argument("--sources", default=",".join(VALID_SOURCES),
                        help="Comma-separated: alert,forecast")
    parser.add_argument("--purge-expired-days", type=int, default=7,
                        help="Delete alerts that expired more than N days ago. "
                             "0 disables (default: 7).")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument(
        "--skip-embed", action="store_true",
        help="Harvest and upsert only, leaving the embedding to a separate run. "
             "The Databricks Job uses this to keep the ONNX runtime and the "
             "HTTP client in different processes - loading requests, psycopg2 "
             "and fastembed into one serverless kernel crashes it, while either "
             "pair on its own is fine.")
    parser.add_argument("--repo-root", default=None,
                        help="Repo root, for runners where __file__ is undefined "
                             "(Databricks serverless). Read before argparse; listed "
                             "here so the parser accepts it.")
    args = parser.parse_args(argv)

    locations = [loc.strip() for loc in args.locations.split("|") if loc.strip()]
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    started = time.time()

    if args.app_url:
        print(f"mode:      app ({args.app_url})")
        print(f"locations: {locations}")
        print(f"sources:   {sources}  limit={args.limit}\n")
        rc = refresh_via_app(args.app_url, locations, args.limit, sources)
        print(f"elapsed:   {time.time() - started:.1f}s")
        return rc

    # -- direct mode: this process talks to Lakebase itself ------------------
    import weather_refresh

    print("mode:      direct (this process connects to Lakebase)")
    print(f"locations: {locations}")
    print(f"sources:   {sources}  limit={args.limit}")
    print(f"secret:    {os.environ['LAKEBASE_SECRET_SCOPE']}/"
          f"{os.environ['LAKEBASE_SECRET_KEY']}"
          f"{'  (overridden by LAKEBASE_URL)' if os.environ.get('LAKEBASE_URL') else ''}\n")

    result = weather_refresh.refresh_once(
        locations=locations,
        limit=args.limit,
        sources=sources,
        purge_expired_days=args.purge_expired_days,
        embed=not args.skip_embed,
        log=print,
    )

    print(f"\nfetched:   {result['fetched']}")
    print(f"upserted:  {result['upserted']} row(s), "
          f"{result['embeddings_invalidated']} stale embedding row(s) invalidated")
    print(f"purged:    {result['purged']} expired alert(s)")
    if args.skip_embed:
        print("embed:     skipped (--skip-embed)")
    else:
        print(f"embedded:  {result['embedded_documents']} doc(s) -> "
              f"{result['embedded_chunks']} chunk(s), {result['embedded_written']} written")
    for err in result["errors"]:
        print(f"  ! {err}")
    print(f"\nafter:     {result['stats']}")
    print(f"elapsed:   {result['elapsed_seconds']}s")

    if not args.skip_embed and result["stats"]["pending"]:
        print(f"\nWARNING: {result['stats']['pending']} document(s) still unembedded")
        return 1
    if result["errors"] and result["fetched"] == 0:
        print("\nERROR: every location failed upstream")
        return 1
    return 0


if __name__ == "__main__":
    # Deliberately NOT `raise SystemExit(main())`. Databricks serverless runs
    # this through exec() inside an IPython kernel, where a SystemExit - even
    # SystemExit(0) - tears the kernel down mid-flight. The task is then marked
    # failed with "The Python kernel is unresponsive" and, worse, the buffered
    # stdout is lost, so the run reports no logs at all. Raising only on a
    # non-zero result keeps a real failure visible to the job while letting a
    # successful run end cleanly under both runners.
    _rc = main()
    if _rc:
        raise SystemExit(_rc)
