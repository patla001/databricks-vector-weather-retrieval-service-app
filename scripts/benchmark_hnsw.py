"""
HNSW index benchmark: query latency with the index vs. a sequential scan.

    python scripts/benchmark_hnsw.py --runs 30

Rather than DROP INDEX (destructive, slow to rebuild, and a bad idea against a
shared database), this toggles the planner per-session:

    SET LOCAL enable_indexscan = off;   -- forces the sequential-scan baseline

Both paths run the exact query weather_search.search_weather issues, so the
numbers describe the endpoint rather than a synthetic stand-in.

Read the output honestly. Postgres only chooses an index when it believes the
index is cheaper, and on a few thousand rows a sequential scan over a 384-dim
vector column often IS cheaper - the planner ignoring HNSW at small scale is
correct behavior, not a misconfiguration. EXPLAIN output for both paths is
printed so you can see which plan actually ran.
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time

# Matches the inlined vector literal Postgres echoes back in EXPLAIN output.
_VECTOR_LITERAL_RE = re.compile(r"'\[[-0-9eE.,+\s]{200,}\]'")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:  # pragma: no cover
    pass

import lakebase
import weather_pipeline

QUERIES = [
    "flash flood risk this weekend",
    "severe thunderstorm with damaging wind gusts",
    "extreme heat advisory for the afternoon",
    "coastal small craft advisory and rough seas",
    "sunny and mild with light winds",
]

SQL = f"""
    SELECT d.id, d.location, e.chunk_text,
           1 - (e.embedding <=> %(vec)s::vector) AS similarity
    FROM {weather_pipeline.DEFAULT_EMBEDDINGS_TABLE} e
    JOIN {weather_pipeline.DEFAULT_DOCUMENTS_TABLE} d ON d.id = e.document_id
    ORDER BY e.embedding <=> %(vec)s::vector
    LIMIT %(top_k)s
"""


def _time_queries(vectors, top_k, use_index, runs, sql=SQL):
    """Return per-query wall-clock milliseconds over `runs` iterations."""
    timings = []
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            for i in range(runs):
                vec = vectors[i % len(vectors)]
                # SET LOCAL scopes to the transaction, so the setting can never
                # leak into another session.
                cur.execute("BEGIN")
                if not use_index:
                    cur.execute("SET LOCAL enable_indexscan = off")
                    cur.execute("SET LOCAL enable_bitmapscan = off")
                start = time.perf_counter()
                cur.execute(sql, {"vec": vec, "top_k": top_k})
                cur.fetchall()
                timings.append((time.perf_counter() - start) * 1000.0)
                cur.execute("COMMIT")
    return timings


def _explain(vec, top_k, use_index, sql=SQL):
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            if not use_index:
                cur.execute("SET LOCAL enable_indexscan = off")
                cur.execute("SET LOCAL enable_bitmapscan = off")
            cur.execute("EXPLAIN ANALYZE " + sql, {"vec": vec, "top_k": top_k})
            plan = [list(row.values())[0] for row in cur.fetchall()]
            cur.execute("COMMIT")
    # The Sort Key line embeds the whole 384-dim query vector, which is several
    # thousand unreadable characters. Collapse it so the plan stays scannable -
    # the interesting part is which scan node was chosen, not the literal.
    return [_VECTOR_LITERAL_RE.sub("'[<384-dim query vector>]'", line) for line in plan]


BENCH_TABLE = "weather_embeddings_bench"

BENCH_SQL = f"""
    SELECT id, 1 - (embedding <=> %(vec)s::vector) AS similarity
    FROM {BENCH_TABLE}
    ORDER BY embedding <=> %(vec)s::vector
    LIMIT %(top_k)s
"""


def build_synthetic(rows: int, dims: int = 384):
    """Populate a scratch table with `rows` random unit vectors, HNSW-indexed.

    The real corpus is a few hundred rows, which is below the size where an
    index beats a scan - so measuring only against it can show the crossover
    is real. This builds a throwaway table big enough to cross it, and
    drop_synthetic() removes it afterwards. It never touches weather_embeddings.
    """
    print(f"building synthetic corpus: {rows} x {dims}-dim vectors ...")
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {BENCH_TABLE}")
            cur.execute(
                f"CREATE TABLE {BENCH_TABLE} "
                f"(id BIGSERIAL PRIMARY KEY, embedding VECTOR({dims}) NOT NULL)"
            )
            # Generated server-side so several hundred thousand floats never
            # cross the wire. l2_normalize keeps them on the unit sphere, which
            # is the distribution the real sentence embeddings have.
            cur.execute(
                f"""
                INSERT INTO {BENCH_TABLE} (embedding)
                SELECT l2_normalize(
                    (SELECT array_agg(random() - 0.5) FROM generate_series(1, {dims}))::vector
                )
                FROM generate_series(1, %s)
                """,
                (rows,),
            )
            conn.commit()
            print("  building HNSW index ...")
            cur.execute(
                f"CREATE INDEX {BENCH_TABLE}_hnsw ON {BENCH_TABLE} "
                f"USING hnsw (embedding vector_cosine_ops)"
            )
            cur.execute(f"ANALYZE {BENCH_TABLE}")
            conn.commit()
    print("  done\n")


def drop_synthetic():
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {BENCH_TABLE}")
            conn.commit()


def _report(label, timings):
    timings = sorted(timings)
    p50 = statistics.median(timings)
    p95 = timings[min(len(timings) - 1, int(len(timings) * 0.95))]
    print(
        f"  {label:<22} n={len(timings):<4} "
        f"min={timings[0]:7.2f}ms  p50={p50:7.2f}ms  p95={p95:7.2f}ms  max={timings[-1]:7.2f}ms"
    )
    return p50


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=30, help="Timed queries per mode.")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=5,
                        help="Untimed queries first, so caches are warm for both modes.")
    parser.add_argument(
        "--synthetic", type=int, metavar="N", default=None,
        help="Also benchmark a throwaway table of N random vectors, to show the "
             "corpus size at which HNSW starts winning. Try 50000. Dropped afterwards.",
    )
    args = parser.parse_args(argv)

    import weather_search

    counts = weather_pipeline.summarize()
    print(f"corpus: {counts['embeddings']} embedding row(s) "
          f"over {counts['documents']} document(s)\n")
    if counts["embeddings"] == 0:
        print("Nothing to benchmark - sync and embed first.")
        return 1

    indexes = lakebase.run_query(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s",
        (weather_pipeline.DEFAULT_EMBEDDINGS_TABLE,),
    )
    hnsw = [i for i in indexes if "hnsw" in i["indexdef"].lower()]
    print("HNSW index: " + (hnsw[0]["indexname"] if hnsw else "NOT PRESENT - run sql/02"))
    print()

    vectors = [weather_pipeline.to_vector_literal(weather_search.embed_query(q))
               for q in QUERIES]

    # Warm the shared buffers so the first mode measured isn't unfairly slow.
    _time_queries(vectors, args.top_k, True, args.warmup)
    _time_queries(vectors, args.top_k, False, args.warmup)

    print("latency:")
    with_index = _time_queries(vectors, args.top_k, True, args.runs)
    without = _time_queries(vectors, args.top_k, False, args.runs)
    p50_with = _report("index allowed", with_index)
    p50_without = _report("seqscan forced", without)

    print()
    if p50_with < p50_without:
        print(f"  -> index path is {p50_without / p50_with:.2f}x faster at p50")
    else:
        print(f"  -> NO speedup at this corpus size "
              f"({p50_with / p50_without:.2f}x slower at p50).")
        print("     Expected on a small table: a sequential scan over a few thousand")
        print("     vectors beats an index walk, and the planner knows it. Re-run after")
        print("     the corpus grows to see HNSW win.")

    print("\nplan with index allowed:")
    for line in _explain(vectors[0], args.top_k, True):
        print("  " + line)
    print("\nplan with seqscan forced:")
    for line in _explain(vectors[0], args.top_k, False):
        print("  " + line)

    if args.synthetic:
        print("\n" + "=" * 68)
        print(f"synthetic scale test: {args.synthetic} rows")
        print("=" * 68)
        try:
            build_synthetic(args.synthetic)
            _time_queries(vectors, args.top_k, True, args.warmup, BENCH_SQL)
            _time_queries(vectors, args.top_k, False, args.warmup, BENCH_SQL)
            print("latency:")
            s_with = _time_queries(vectors, args.top_k, True, args.runs, BENCH_SQL)
            s_without = _time_queries(vectors, args.top_k, False, args.runs, BENCH_SQL)
            p50_w = _report("index allowed", s_with)
            p50_wo = _report("seqscan forced", s_without)
            print()
            if p50_w < p50_wo:
                print(f"  -> index path is {p50_wo / p50_w:.2f}x faster at p50 "
                      f"({args.synthetic} rows)")
            else:
                print(f"  -> still no speedup at {args.synthetic} rows "
                      f"({p50_w / p50_wo:.2f}x)")
            print("\nplan with index allowed:")
            for line in _explain(vectors[0], args.top_k, True, BENCH_SQL):
                print("  " + line)
        finally:
            drop_synthetic()
            print(f"\ndropped {BENCH_TABLE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
