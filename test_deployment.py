"""
End-to-end test for the weather retrieval service.

    python test_deployment.py http://localhost:8000

Every write is verified twice: once through the HTTP API, and once by
connecting straight to Lakebase with psycopg2. An app that cached in memory, or
wrote somewhere else entirely, passes the first check and fails the second.

Exits non-zero on any failure, so it works as a CI gate.
"""

from __future__ import annotations

import sys
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

import lakebase
import weather_pipeline

passed = 0
failed = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {label}" + (f" - {detail}" if detail else ""))
    else:
        failed += 1
        print(f"[FAIL] {label}" + (f" - {detail}" if detail else ""))
    return ok


def body(resp) -> dict:
    """Parse a JSON body, returning {} rather than raising on anything else.

    A deployed app can answer with HTML (an auth redirect) or an empty body (a
    platform health check) where JSON was expected. Those should show up as
    failed checks with a readable detail line, not as a traceback that abandons
    the rest of the suite.
    """
    try:
        return resp.json()
    except ValueError:
        return {}


def describe(resp) -> str:
    """Short, safe description of a response for a failure detail line."""
    ctype = (resp.headers.get("content-type") or "none").split(";")[0]
    return f"HTTP {resp.status_code}, content-type {ctype}, {len(resp.content)}B"


def main(base_url: str) -> int:
    base_url = base_url.rstrip("/")
    session = requests.Session()

    # Databricks Apps sit behind OAuth; locally this is a no-op.
    try:
        from databricks.sdk import WorkspaceClient

        session.headers.update(WorkspaceClient().config.authenticate())
    except Exception:
        pass

    print(f"target: {base_url}\n")

    # -- health ------------------------------------------------------------
    # On Databricks Apps, /healthz is claimed by the PLATFORM as its own health
    # probe: it answers 200 with an empty body and no content-type, and the
    # request never reaches Flask. Locally the same path returns
    # {"status": "ok"} from app.py. Accept either - a 200 means alive on both.
    r = session.get(f"{base_url}/healthz", timeout=30)
    served_by_flask = body(r).get("status") == "ok"
    check(r.status_code == 200, "GET /healthz", describe(r)
          + (" (Flask)" if served_by_flask else " (platform probe)"))

    # /api is app code on every host, so it is the real liveness check for the
    # deployed service. (It used to be "/", which now serves the console.)
    r = session.get(f"{base_url}/api", timeout=30)
    check(r.status_code == 200 and "endpoints" in body(r),
          "GET /api (app is actually serving)", describe(r))
    ui_built = body(r).get("ui") == "built"

    # -- console -----------------------------------------------------------
    # The console is a Next.js static export copied into static/. If it has not
    # been built, "/" falls back to the JSON index rather than 404ing, so both
    # states are legitimate - but say which one this deployment is in.
    r = session.get(f"{base_url}/", timeout=30)
    is_html = "text/html" in (r.headers.get("content-type") or "")
    if ui_built:
        check(r.status_code == 200 and is_html, "GET / serves the console", describe(r))
        r = session.get(f"{base_url}/static/geo.json", timeout=30)
        check(r.status_code == 200, "console map geometry is served", describe(r))
    else:
        check(r.status_code == 200 and not is_html,
              "GET / falls back to the API index (console not built)", describe(r))

    # -- sync --------------------------------------------------------------
    before = weather_pipeline.summarize()

    r = session.post(
        f"{base_url}/weather/sync",
        json={"locations": ["Chicago, IL", "Miami, FL"], "limit": 25},
        timeout=300,
    )
    ok = check(r.status_code == 200, "POST /weather/sync", f"{r.status_code}")
    if ok:
        data = body(r)
        check("synced" in data and isinstance(data["synced"], int),
              "  sync response has an integer 'synced'", str(data.get("synced")))
        check(bool(data.get("by_source")),
              "  sync response breaks down by source_type", str(data.get("by_source")))

        # Second check: go straight to Postgres. The API said it wrote rows;
        # confirm they are actually there.
        after = weather_pipeline.summarize()
        check(after["documents"] >= before["documents"],
              "  documents present in Lakebase (direct psycopg2)",
              f"{before['documents']} -> {after['documents']}")
        check(after["alerts"] + after["forecasts"] == after["documents"],
              "  every document has a valid source_type",
              f"{after['alerts']} alerts + {after['forecasts']} forecasts")

    # -- idempotence -------------------------------------------------------
    # Asserting the row count is unchanged would be wrong: NWS is a live feed,
    # and a warning issued between the two syncs legitimately adds a row. The
    # real invariant is that re-syncing UPSERTS rather than INSERTS - every id
    # seen before is still present exactly once afterwards.
    ids_before = {r["id"] for r in lakebase.run_query(
        f"SELECT id FROM {weather_pipeline.DEFAULT_DOCUMENTS_TABLE}")}

    session.post(
        f"{base_url}/weather/sync",
        json={"locations": ["Chicago, IL", "Miami, FL"], "limit": 25},
        timeout=300,
    )

    rows = lakebase.run_query(
        f"SELECT count(*) AS total, count(DISTINCT id) AS distinct_ids "
        f"FROM {weather_pipeline.DEFAULT_DOCUMENTS_TABLE}")[0]
    check(rows["total"] == rows["distinct_ids"],
          "re-running sync creates no duplicate documents",
          f"{rows['total']} rows / {rows['distinct_ids']} distinct ids")

    ids_after = {r["id"] for r in lakebase.run_query(
        f"SELECT id FROM {weather_pipeline.DEFAULT_DOCUMENTS_TABLE}")}
    check(ids_before <= ids_after,
          "  re-sync preserves every previously-synced document",
          f"{len(ids_before - ids_after)} lost")
    new_ids = ids_after - ids_before
    if new_ids:
        print(f"       ({len(new_ids)} genuinely new upstream document(s) arrived "
              f"between syncs - expected on a live feed)")

    # -- schema invariants (direct SQL) ------------------------------------
    orphans = lakebase.run_query(f"""
        SELECT COUNT(*) AS n FROM {weather_pipeline.DEFAULT_EMBEDDINGS_TABLE} e
        LEFT JOIN {weather_pipeline.DEFAULT_DOCUMENTS_TABLE} d ON d.id = e.document_id
        WHERE d.id IS NULL""")[0]["n"]
    check(orphans == 0, "no orphan embeddings", f"{orphans} orphan(s)")

    dupes = lakebase.run_query(f"""
        SELECT COUNT(*) AS n FROM (
          SELECT document_id, chunk_index FROM {weather_pipeline.DEFAULT_EMBEDDINGS_TABLE}
          GROUP BY 1, 2 HAVING COUNT(*) > 1) x""")[0]["n"]
    check(dupes == 0, "no duplicate (document_id, chunk_index)", f"{dupes} duplicate(s)")

    stats = weather_pipeline.summarize()
    if stats["embeddings"] == 0:
        print("\n[SKIP] search checks - nothing embedded yet. "
              "Run: python notebooks/ingest_weather_embeddings.py\n")
    else:
        dims = lakebase.run_query(
            f"SELECT DISTINCT vector_dims(embedding) AS d "
            f"FROM {weather_pipeline.DEFAULT_EMBEDDINGS_TABLE}")
        check(len(dims) == 1 and dims[0]["d"] == 384,
              "every stored vector is 384-dim", str([d["d"] for d in dims]))

        models = lakebase.run_query(
            f"SELECT DISTINCT model_name FROM {weather_pipeline.DEFAULT_EMBEDDINGS_TABLE}")
        check(len(models) == 1, "all vectors come from one model",
              str([m["model_name"] for m in models]))

        # -- search --------------------------------------------------------
        r = session.post(f"{base_url}/weather/search",
                         json={"query": "flash flood risk this weekend", "top_k": 5},
                         timeout=120)
        ok = check(r.status_code == 200, "POST /weather/search", f"{r.status_code}")
        if ok:
            data = body(r)
            results = data.get("results", [])
            check(len(results) > 0, "  search returns results", f"{len(results)} hit(s)")
            if results:
                first = results[0]
                for field in ("location", "headline", "chunk_text", "similarity"):
                    check(field in first, f"  result carries '{field}'")
                sims = [x["similarity"] for x in results]
                check(sims == sorted(sims, reverse=True),
                      "  results are ranked by descending similarity", str(sims[:3]))
                check(all(-1.0 <= float(s) <= 1.0 for s in sims),
                      "  similarities are in [-1, 1]")

        # filter
        r = session.post(f"{base_url}/weather/search",
                         json={"query": "sunny", "top_k": 5, "source_type": "forecast"},
                         timeout=120)
        if check(r.status_code == 200, "POST /weather/search (source_type filter)"):
            rows = body(r).get("results", [])
            check(all(x["source_type"] == "forecast" for x in rows),
                  "  filter returns only forecasts", f"{len(rows)} row(s)")

        # GET variant
        r = session.get(f"{base_url}/weather/search",
                        params={"query": "heat advisory", "top_k": 3}, timeout=120)
        check(r.status_code == 200 and body(r).get("count", 0) > 0,
              "GET /weather/search", describe(r))

    # -- map view ----------------------------------------------------------
    r = session.get(f"{base_url}/weather/map?limit=500", timeout=90)
    if check(r.status_code == 200, "GET /weather/map", describe(r)):
        features = body(r).get("features", [])
        check(len(features) > 0, "  map returns features", f"{len(features)} feature(s)")
        check(all(f.get("latitude") is not None for f in features),
              "  every feature carries coordinates")
        # Thinned polygons must use `rings`, the shape the console draws. A raw
        # GeoJSON `coordinates` here renders nothing and throws client-side.
        polygons = [f["geometry"] for f in features if f.get("geometry")]
        check(all("rings" in g for g in polygons),
              "  polygons are thinned to rings", f"{len(polygons)} polygon(s)")
        check(all("narrative_text" not in f for f in features),
              "  map payload omits narrative bodies")

        if features:
            doc_id = quote(features[0]["id"], safe="")
            r = session.get(f"{base_url}/weather/document/{doc_id}", timeout=30)
            if check(r.status_code == 200, "GET /weather/document/<id>", describe(r)):
                check("narrative_text" in body(r), "  document carries the narrative")

    # Search results have to be plottable, and in the same shape as the map's.
    r = session.post(f"{base_url}/weather/search",
                     json={"query": "flooding", "top_k": 5}, timeout=120)
    if r.status_code == 200 and body(r).get("results"):
        rows = body(r)["results"]
        check(all(row.get("latitude") is not None for row in rows),
              "search results carry coordinates (the globe needs them)")
        check(all(isinstance(row["similarity"], (int, float)) for row in rows),
              "similarity is a JSON number, not a string",
              f"got {type(rows[0]['similarity']).__name__}")
        geoms = [row["geometry"] for row in rows if row.get("geometry")]
        check(all("rings" in g for g in geoms),
              "search geometry matches the map's shape", f"{len(geoms)} polygon(s)")

    # -- edge cases --------------------------------------------------------
    r = session.post(f"{base_url}/weather/search", json={}, timeout=60)
    check(r.status_code == 400, "missing query -> 400", f"{r.status_code}")

    # An unbounded query is a real cost hole, not just bad input: it goes into
    # the summary prompt verbatim and is billed as input tokens.
    r = session.post(f"{base_url}/weather/search",
                     json={"query": "flood " * 400}, timeout=60)
    check(r.status_code == 400, "oversized query -> 400", describe(r))
    r = session.get(f"{base_url}/weather/search",
                    params={"query": "flood " * 400}, timeout=60)
    check(r.status_code == 400, "oversized query -> 400 (GET too)", describe(r))

    r = session.post(f"{base_url}/weather/search",
                     json={"query": "x", "top_k": 9999}, timeout=120)
    check(r.status_code == 200 and body(r).get("top_k") == 20,
          "top_k=9999 clamps to 20", str(body(r).get("top_k")))

    r = session.post(f"{base_url}/weather/search",
                     json={"query": "x", "source_type": "tornado"}, timeout=60)
    check(r.status_code == 400, "invalid source_type -> 400", f"{r.status_code}")

    r = session.post(f"{base_url}/weather/sync",
                     json={"locations": ["Nowhere, ZZ"]}, timeout=60)
    check(r.status_code == 400, "unknown location -> 400", f"{r.status_code}")

    # -- summary guardrails -------------------------------------------------
    r = session.get(f"{base_url}/weather/stats", timeout=60)
    budget = body(r).get("summary") or {}
    if check(bool(budget), "stats carries the summary budget"):
        check(isinstance(budget.get("daily_limit"), int),
              "  daily ceiling is configured", f"limit={budget.get('daily_limit')}")
        check("enabled" in budget,
              "  reports whether answers are configured",
              "on" if budget.get("enabled") else "off (ANTHROPIC_API_KEY unset)")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    raise SystemExit(main(url))
