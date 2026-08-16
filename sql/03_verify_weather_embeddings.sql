-- RUN IN THE LAKEBASE SQL EDITOR (from the database instance page).
--
-- Post-run verification for the weather pipeline. Every query below states the
-- result you should expect; anything else is a real problem, not a warning.

-- 1. Corpus size by source. Expect both 'alert' and 'forecast' to be non-zero
--    after a sync that requested both. A zero alert count is not necessarily a
--    bug - it can simply be a calm day nationwide.
SELECT source_type, COUNT(*) AS documents
FROM weather_documents
GROUP BY source_type
ORDER BY source_type;

-- 2. Chunk fan-out. Forecast text is short enough to always be one chunk;
--    long alerts split. Expect avg_chunks_per_doc = 1.0 for 'forecast' and
--    > 1.0 for 'alert'.
SELECT e.source_type,
       COUNT(*)                                  AS chunks,
       COUNT(DISTINCT e.document_id)             AS documents,
       ROUND(COUNT(*)::numeric
             / NULLIF(COUNT(DISTINCT e.document_id), 0), 2) AS avg_chunks_per_doc,
       MAX(e.chunk_index) + 1                    AS max_chunks_in_one_doc
FROM weather_embeddings e
GROUP BY e.source_type
ORDER BY e.source_type;

-- 3. Orphan check. MUST return 0. The FK makes this impossible, so a non-zero
--    result means the constraint was dropped or the table was rebuilt without it.
SELECT COUNT(*) AS orphan_embeddings
FROM weather_embeddings e
LEFT JOIN weather_documents d ON d.id = e.document_id
WHERE d.id IS NULL;

-- 4. Unembedded backlog. MUST return 0 immediately after the embed job.
--    Non-zero means the job stopped early or the anti-join found new work.
SELECT COUNT(*) AS documents_awaiting_embedding
FROM weather_documents d
LEFT JOIN weather_embeddings e ON e.document_id = d.id
WHERE e.id IS NULL;

-- 5. Dimension check. Expect exactly one row: 384 (or whatever your model emits).
--    vector_dims() reads the actual stored vector, not the column declaration,
--    so this catches a table that was created at the wrong width.
SELECT DISTINCT vector_dims(embedding) AS dims
FROM weather_embeddings;

-- 6. Provenance. Expect exactly one model_name. More than one means vectors
--    from different models share a table, and their cosine distances are
--    not comparable.
SELECT model_name, COUNT(*) AS vectors, MIN(created_at) AS first, MAX(created_at) AS last
FROM weather_embeddings
GROUP BY model_name;

-- 7. Duplicate check. MUST return no rows - the PK and the UNIQUE constraint
--    both forbid it, so this confirms re-running the sync did not duplicate.
SELECT document_id, chunk_index, COUNT(*)
FROM weather_embeddings
GROUP BY document_id, chunk_index
HAVING COUNT(*) > 1;

-- 8. Confirm the HNSW index exists and uses cosine ops. Expect one row
--    containing "USING hnsw" and "vector_cosine_ops".
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'weather_embeddings'
ORDER BY indexname;

-- 9. Smoke-test the distance operator end to end: nearest neighbours of an
--    existing vector. The first row should be the vector itself, similarity 1.0.
SELECT e.document_id,
       d.location,
       d.event,
       ROUND((1 - (e.embedding <=> (SELECT embedding FROM weather_embeddings LIMIT 1)))::numeric, 4)
           AS similarity
FROM weather_embeddings e
JOIN weather_documents d ON d.id = e.document_id
ORDER BY e.embedding <=> (SELECT embedding FROM weather_embeddings LIMIT 1)
LIMIT 5;
