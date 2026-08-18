"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import type {
  BenchmarkResult, EmbedResult, RefreshResult, RefreshStatus, Stats, SyncResult,
} from "@/lib/types";

interface Props {
  stats: Stats | null;
  refresh: RefreshStatus | null;
  onClose: () => void;
  onChanged: () => void;
}

type Busy = null | "sync" | "embed" | "refresh" | "bench";

export default function PipelinePanel({ stats, refresh, onClose, onChanged }: Props) {
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [sync, setSync] = useState<SyncResult | null>(null);
  const [embed, setEmbed] = useState<EmbedResult | null>(null);
  const [cycle, setCycle] = useState<RefreshResult | null>(null);
  const [bench, setBench] = useState<BenchmarkResult | null>(null);

  async function run<T>(kind: Exclude<Busy, null>, fn: () => Promise<T>, set: (v: T) => void) {
    setBusy(kind);
    setError(null);
    try {
      set(await fn());
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <aside className="drawer">
      <div className="drawer-head">
        <h2>Pipeline</h2>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close pipeline">
          ×
        </button>
      </div>

      <div className="drawer-body">
        {error && <div className="err" style={{ marginTop: 14 }}>{error}</div>}

        {/* ---- Part 1 ---------------------------------------------------- */}
        <section className="stage">
          <div className="stage-head">
            <h3>Harvest</h3>
            <span className="tag">POST /weather/sync</span>
          </div>
          <p className="why">
            Pulls active alerts and narrative forecasts from api.weather.gov and upserts them on
            their natural id. Re-running never duplicates: a document that came back unchanged is
            updated in place, and one whose text actually changed has its stale vectors deleted so
            the next embed pass rewrites them.
          </p>
          <div className="row">
            <button
              type="button"
              className="btn"
              disabled={busy !== null}
              onClick={() => run("sync", () => api.sync({ limit: 50 }), setSync)}
            >
              {busy === "sync" ? <span className="spinner" /> : "Run harvest"}
            </button>
            <span className="num" style={{ fontSize: 11, color: "var(--faint)" }}>
              {stats ? `${stats.alerts} alerts · ${stats.forecasts} forecasts` : ""}
            </span>
          </div>
          {sync && (
            <div className="readout">
              <span className="k">synced      </span>{sync.synced}
              {"\n"}
              <span className="k">by source   </span>
              {Object.entries(sync.by_source ?? {}).map(([k, v]) => `${k}=${v}`).join("  ")}
              {"\n"}
              <span className="k">invalidated </span>
              <span className={sync.embeddings_invalidated ? "warn" : ""}>
                {sync.embeddings_invalidated ?? 0}
              </span>
              {sync.embeddings_invalidated
                ? "  ← revised upstream, vectors dropped for re-embedding"
                : "  ← nothing changed under existing vectors"}
              {sync.errors?.length ? `\n\n${sync.errors.join("\n")}` : ""}
            </div>
          )}
        </section>

        {/* ---- Part 2 ---------------------------------------------------- */}
        <section className="stage">
          <div className="stage-head">
            <h3>Vectorize</h3>
            <span className="tag">POST /weather/embed</span>
          </div>
          <p className="why">
            Chunks at 800 characters with 100 of overlap, embeds with all-MiniLM-L6-v2 (384-dim,
            ONNX) and writes through psycopg2 <code>execute_values</code> with an explicit
            <code> ::vector</code> cast. Only documents with no vectors are picked up, so this is
            safe to run repeatedly.
          </p>
          <div className="row">
            <button
              type="button"
              className="btn"
              disabled={busy !== null || stats?.pending === 0}
              onClick={() => run("embed", () => api.embed({}), setEmbed)}
            >
              {busy === "embed" ? <span className="spinner" /> : "Embed backlog"}
            </button>
            <span className="num" style={{ fontSize: 11, color: stats?.pending ? "var(--severe)" : "var(--faint)" }}>
              {stats ? `${stats.pending} pending · ${stats.embeddings} vectors` : ""}
            </span>
          </div>
          {embed && (
            <div className="readout">
              <span className="k">documents </span>{embed.documents}
              {"\n"}
              <span className="k">chunks    </span>{embed.chunks}
              {"\n"}
              <span className="k">written   </span>
              <span className="win">{embed.written}</span>
              {"\n"}
              <span className="k">remaining </span>{embed.remaining ?? 0}
              {embed.note ? `\n\n${embed.note}` : ""}
            </div>
          )}
        </section>

        {/* ---- Stretch: scheduled re-sync -------------------------------- */}
        <section className="stage">
          <div className="stage-head">
            <h3>Scheduled refresh</h3>
            <span className="tag">weather_scheduler</span>
          </div>
          <p className="why">
            Active NWS alerts expire within hours, so the corpus needs re-harvesting on a timer.
            This runs in the app process rather than as a Databricks Job: the workspace is
            serverless-only, and a serverless task that loads requests, psycopg2 and fastembed
            into one kernel segfaults it. The app already runs that combination to serve search.
          </p>
          <div className="row">
            <button
              type="button"
              className="btn"
              disabled={busy !== null || refresh?.running}
              onClick={() => run("refresh", () => api.refreshNow({}), setCycle)}
            >
              {busy === "refresh" ? <span className="spinner" /> : "Force a cycle"}
            </button>
            <span className="num" style={{ fontSize: 11, color: "var(--faint)" }}>
              {refresh
                ? `every ${refresh.interval_minutes}m · ${refresh.cycles} cycles · ${refresh.failures} failed`
                : ""}
            </span>
          </div>
          {(cycle ?? refresh?.last_result) && (
            <Cycle result={(cycle ?? refresh?.last_result)!} live={Boolean(cycle)} />
          )}
        </section>

        {/* ---- Stretch: HNSW benchmark ----------------------------------- */}
        <section className="stage">
          <div className="stage-head">
            <h3>Index benchmark</h3>
            <span className="tag">POST /weather/benchmark</span>
          </div>
          <p className="why">
            Times the HNSW path against a forced sequential scan by toggling the planner per
            transaction, rather than dropping and rebuilding the index. Expect no speedup at this
            corpus size — a few hundred rows are cheaper to scan than to descend a graph for, and
            the readout says which plan Postgres actually chose.
          </p>
          <div className="row">
            <button
              type="button"
              className="btn"
              disabled={busy !== null}
              onClick={() => run("bench", () => api.benchmark({ runs: 40 }), setBench)}
            >
              {busy === "bench" ? <span className="spinner" /> : "Run benchmark"}
            </button>
            <span className="num" style={{ fontSize: 11, color: "var(--faint)" }}>
              ~15s over {stats?.embeddings ?? 0} vectors
            </span>
          </div>
          {bench && (
            <div className="readout">
              <span className="k">rows          </span>{bench.rows}
              {"\n"}
              <span className="k">index allowed </span>
              p50 {bench.index_allowed.p50_ms}ms  p95 {bench.index_allowed.p95_ms}ms
              {"\n"}
              <span className="k">seqscan forced</span>
              {"  "}p50 {bench.seqscan_forced.p50_ms}ms  p95 {bench.seqscan_forced.p95_ms}ms
              {"\n"}
              <span className="k">plan chosen   </span>
              <span className={bench.index_allowed.uses_index ? "win" : "warn"}>
                {bench.index_allowed.uses_index ? "Index Scan (HNSW)" : "Seq Scan"}
              </span>
              {"\n\n"}
              {bench.verdict}
            </div>
          )}
        </section>
      </div>
    </aside>
  );
}

function Cycle({ result, live }: { result: RefreshResult; live: boolean }) {
  return (
    <div className="readout">
      <span className="k">{live ? "this cycle" : "last cycle"}</span>
      {"\n"}
      <span className="k">fetched     </span>{result.fetched}
      {"\n"}
      <span className="k">upserted    </span>{result.upserted}
      {"\n"}
      <span className="k">invalidated </span>
      <span className={result.embeddings_invalidated ? "warn" : ""}>
        {result.embeddings_invalidated}
      </span>
      {"\n"}
      <span className="k">purged      </span>{result.purged}
      {"\n"}
      <span className="k">embedded    </span>
      <span className="win">{result.embedded_written}</span>
      {"\n"}
      <span className="k">elapsed     </span>{result.elapsed_seconds}s
      {result.errors?.length ? `\n\n${result.errors.join("\n")}` : ""}
    </div>
  );
}
