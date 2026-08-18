export type SourceType = "alert" | "forecast";

/** Only the outer rings survive the server's thinning; holes are dropped. */
export interface Geometry {
  type: "MultiPolygon";
  rings: [number, number][][];
}

export interface Feature {
  id: string;
  location: string;
  latitude: number | null;
  longitude: number | null;
  source_type: SourceType;
  event: string | null;
  headline: string | null;
  severity: string | null;
  area_desc: string | null;
  issued_at: string | null;
  expires_at: string | null;
  geometry: Geometry | null;
}

export interface SearchHit extends Feature {
  narrative_text: string;
  chunk_index: number;
  chunk_text: string;
  /** Cosine similarity, already rounded to 4dp by Postgres. */
  similarity: number;
}

export interface SearchResponse {
  query: string;
  top_k: number;
  source_type: SourceType | null;
  location: string | null;
  count: number;
  results: SearchHit[];
  note?: string;
  summary?: string;
  summary_error?: string;
}

export interface SummaryBudget {
  model: string;
  /** False when ANTHROPIC_API_KEY is unset — the answer card stays dark. */
  enabled: boolean;
  calls_today: number;
  daily_limit: number;
  /** null when the daily limit is disabled (limit of 0). */
  remaining_today: number | null;
  cache_hits_today: number;
  throttled_today: number;
  cached_summaries: number;
}

export interface Stats {
  documents: number;
  alerts: number;
  forecasts: number;
  embeddings: number;
  pending: number;
  summary?: SummaryBudget;
}

export interface RefreshResult {
  fetched: number;
  upserted: number;
  embeddings_invalidated: number;
  purged: number;
  embedded_documents: number;
  embedded_chunks: number;
  embedded_written: number;
  elapsed_seconds: number;
  errors: string[];
  stats: Stats;
}

export interface RefreshStatus {
  enabled: boolean;
  interval_minutes: number;
  running: boolean;
  cycles: number;
  failures: number;
  last_error: string | null;
  last_started_at?: string | null;
  last_finished_at?: string | null;
  last_result?: RefreshResult | null;
}

export interface SyncResult {
  synced: number;
  locations: string[];
  by_source: Record<string, number>;
  embeddings_invalidated?: number;
  errors?: string[];
}

export interface EmbedResult {
  documents: number;
  chunks: number;
  written: number;
  remaining?: number;
  pending?: number;
  model?: string;
  note?: string;
}

export interface BenchArm {
  p50_ms: number;
  p95_ms: number;
  min_ms: number;
  max_ms: number;
  runs: number;
  plan_node: string;
  uses_index: boolean;
  plan: string[];
}

export interface BenchmarkResult {
  rows: number;
  top_k: number;
  index_allowed: BenchArm;
  seqscan_forced: BenchArm;
  speedup_at_p50: number;
  verdict: string;
}

export interface MapResponse {
  count: number;
  with_polygon: number;
  include_expired: boolean;
  source_type: SourceType | null;
  features: Feature[];
}
