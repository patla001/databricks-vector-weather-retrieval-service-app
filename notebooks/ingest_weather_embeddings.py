"""
Weather embeddings ingestion job.

Reads unembedded rows from weather_documents, chunks the narrative text,
embeds each chunk with all-MiniLM-L6-v2 (384 dims), and writes the vectors into
weather_embeddings via psycopg2 + execute_values.

Runs two ways, unchanged:

    python notebooks/ingest_weather_embeddings.py            # locally, off .env
    (or attach it as a Databricks notebook task)             # off the secret scope

There is deliberately no Spark here. spark.write.jdbc is not supported against
this Lakebase instance, so every write goes through psycopg2 with an explicit
%s::vector cast, batched with execute_values. Throughput is fine: the corpus is
thousands of short documents, and the embedding step dominates the wall clock.

Both tables must already exist - run sql/01 and sql/02 in the Lakebase SQL
editor first. The preflight below checks that and fails loudly rather than
half-writing.
"""

from __future__ import annotations

import argparse
import os
import sys

# Make the repo root importable whether this is run as a script from the repo
# root, from inside notebooks/, or as a Databricks notebook (whose cwd varies).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for path in (_ROOT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:  # pragma: no cover - dotenv is optional on a cluster
    pass

import lakebase
import weather_pipeline

# Expected dimension for the configured model. The embeddings table declares
# VECTOR(384); a mismatch here is the difference between "fails on the first
# INSERT after an expensive embedding run" and "fails in the preflight".
MODEL_DIMS = {
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/all-MiniLM-L12-v2": 384,
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/all-mpnet-base-v2": 768,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
}


def preflight(model_name: str) -> int:
    """Verify both tables exist and the vector column matches the model.

    Returns the column's declared dimension.
    """
    expected = MODEL_DIMS.get(model_name)
    if expected is None:
        raise ValueError(
            f"Unknown embedding model {model_name!r}. Add it to MODEL_DIMS with its "
            "dimension, and make sure sql/02_setup_weather_embeddings.sql declares "
            "VECTOR(<that dimension>)."
        )

    for table in (weather_pipeline.DEFAULT_DOCUMENTS_TABLE,
                  weather_pipeline.DEFAULT_EMBEDDINGS_TABLE):
        rows = lakebase.run_query("SELECT to_regclass(%s) IS NOT NULL AS present", (table,))
        if not rows or not rows[0]["present"]:
            raise RuntimeError(
                f"Table {table!r} does not exist. Run sql/01_setup_weather_documents.sql "
                "and sql/02_setup_weather_embeddings.sql in the Lakebase SQL editor "
                "(from the database instance page) first."
            )

    # atttypmod carries pgvector's declared dimension for a vector column.
    rows = lakebase.run_query(
        """
        SELECT a.atttypmod AS dims
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = %s AND a.attname = 'embedding'
        """,
        (weather_pipeline.DEFAULT_EMBEDDINGS_TABLE,),
    )
    actual = rows[0]["dims"] if rows else None
    if actual != expected:
        raise RuntimeError(
            f"Dimension mismatch: {weather_pipeline.DEFAULT_EMBEDDINGS_TABLE}.embedding is "
            f"VECTOR({actual}) but {model_name} produces {expected}-dim vectors. "
            "Recreate the table at the right width, or change WEATHER_EMBED_MODEL."
        )
    return expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-type",
        choices=["alert", "forecast"],
        default=None,
        help="Only embed documents of this source type (default: both).",
    )
    parser.add_argument("--batch-size", type=int, default=64,
                        help="Chunks per embed + insert batch (default: 64).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap how many pending documents to process this run.")
    parser.add_argument("--chunk-size", type=int, default=weather_pipeline.CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=weather_pipeline.CHUNK_OVERLAP)
    args = parser.parse_args(argv)

    # Imported here so --help works without paying the ONNX runtime import.
    import weather_search

    model_name = weather_search.EMBED_MODEL

    print(f"model:        {model_name}")
    print(f"documents:    {weather_pipeline.DEFAULT_DOCUMENTS_TABLE}")
    print(f"embeddings:   {weather_pipeline.DEFAULT_EMBEDDINGS_TABLE}")
    print(f"chunking:     size={args.chunk_size} overlap={args.chunk_overlap}")
    print()

    dims = preflight(model_name)
    print(f"preflight ok: both tables present, embedding is VECTOR({dims})")

    before = weather_pipeline.summarize()
    print(f"before:       {before}")

    pending = weather_pipeline.pending_documents(
        source_type=args.source_type, limit=args.limit
    )
    print(f"pending:      {len(pending)} document(s) with no embedding")

    if not pending:
        print("nothing to do")
        return 0

    result = weather_pipeline.embed_documents(
        weather_search.embed_texts,
        model_name,
        pending,
        batch_size=args.batch_size,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        log=print,
    )
    print(
        f"embedded:     {result['documents']} document(s) -> {result['chunks']} chunk(s), "
        f"{result['written']} row(s) written "
        f"({result['chunks'] - result['written']} already present)"
    )

    after = weather_pipeline.summarize()
    print(f"after:        {after}")
    if after["pending"]:
        print(f"WARNING: {after['pending']} document(s) still unembedded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
