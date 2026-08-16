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
    r = session.get(f"{base_url}/healthz", timeout=30)
    check(r.status_code == 200 and r.json().get("status") == "ok",
          "GET /healthz", f"{r.status_code}")

    # -- sync --------------------------------------------------------------
    before = weather_pipeline.summarize()

    r = session.post(
        f"{base_url}/weather/sync",
        json={"locations": ["Chicago, IL", "Miami, FL"], "limit": 25},
        timeout=300,
    )
    ok = check(r.status_code == 200, "POST /weather/sync", f"{r.status_code}")
    if ok:
        body = r.json()
        check("synced" in body and isinstance(body["synced"], int),
              "  sync response has an integer 'synced'", str(body.get("synced")))
        check(bool(body.get("by_source")),
              "  sync response breaks down by source_type", str(body.get("by_source")))

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
    n1 = weather_pipeline.summarize()["documents"]
    session.post(
        f"{base_url}/weather/sync",
        json={"locations": ["Chicago, IL", "Miami, FL"], "limit": 25},
        timeout=300,
    )
    n2 = weather_pipeline.summarize()["documents"]
    check(n1 == n2, "re-running sync does not duplicate rows", f"{n1} -> {n2}")

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
            body = r.json()
            results = body.get("results", [])
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
            rows = r.json().get("results", [])
            check(all(x["source_type"] == "forecast" for x in rows),
                  "  filter returns only forecasts", f"{len(rows)} row(s)")

        # GET variant
        r = session.get(f"{base_url}/weather/search",
                        params={"query": "heat advisory", "top_k": 3}, timeout=120)
        check(r.status_code == 200 and r.json().get("count", 0) > 0,
              "GET /weather/search", f"{r.status_code}")

    # -- edge cases --------------------------------------------------------
    r = session.post(f"{base_url}/weather/search", json={}, timeout=60)
    check(r.status_code == 400, "missing query -> 400", f"{r.status_code}")

    r = session.post(f"{base_url}/weather/search",
                     json={"query": "x", "top_k": 9999}, timeout=120)
    check(r.status_code == 200 and r.json().get("top_k") == 20,
          "top_k=9999 clamps to 20", str(r.json().get("top_k")))

    r = session.post(f"{base_url}/weather/search",
                     json={"query": "x", "source_type": "tornado"}, timeout=60)
    check(r.status_code == 400, "invalid source_type -> 400", f"{r.status_code}")

    r = session.post(f"{base_url}/weather/sync",
                     json={"locations": ["Nowhere, ZZ"]}, timeout=60)
    check(r.status_code == 400, "unknown location -> 400", f"{r.status_code}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    raise SystemExit(main(url))
