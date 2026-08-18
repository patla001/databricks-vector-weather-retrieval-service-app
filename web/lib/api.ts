/**
 * Flask API client.
 *
 * Every path is relative. The console is served by the same Flask process that
 * serves the API, so relative URLs inherit the origin - and, on Databricks
 * Apps, the OAuth session cookie the platform already set. An absolute URL to
 * the app from anywhere else gets the proxy's sign-in page instead of JSON.
 *
 * NEXT_PUBLIC_API_BASE exists only for `next dev`, where the console runs on
 * :3000 and Flask on :8000.
 */
import type {
  BenchmarkResult,
  EmbedResult,
  Feature,
  MapResponse,
  RefreshResult,
  RefreshStatus,
  SearchResponse,
  SourceType,
  Stats,
  SyncResult,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError("Can't reach the API. Is the Flask app running?", 0);
  }

  // A Databricks App answers an expired session with its sign-in page as HTML
  // and HTTP 200, so a status check alone would call that a success and the
  // failure would surface as a JSON parse error further down.
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    throw new ApiError(
      response.status === 200
        ? "The API returned a page instead of JSON — the Databricks session has probably expired. Reload to sign in again."
        : `${path} failed with HTTP ${response.status}.`,
      response.status
    );
  }

  const body = await response.json();
  if (!response.ok) {
    throw new ApiError(body?.error ?? `${path} failed with HTTP ${response.status}.`, response.status);
  }
  return body as T;
}

const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });

export const api = {
  stats: () => request<Stats>("/weather/stats"),

  refreshStatus: () => request<RefreshStatus>("/weather/refresh/status"),

  map: (opts: { sourceType?: SourceType | null; includeExpired?: boolean; limit?: number } = {}) => {
    const params = new URLSearchParams();
    if (opts.sourceType) params.set("source_type", opts.sourceType);
    if (opts.includeExpired) params.set("include_expired", "true");
    params.set("limit", String(opts.limit ?? 1500));
    return request<MapResponse>(`/weather/map?${params}`);
  },

  document: (id: string) => request<Feature & { narrative_text: string }>(
    `/weather/document/${encodeURIComponent(id)}`
  ),

  /**
   * GET rather than POST when a summary is wanted: the RAG variant is defined
   * on the GET route, so asking for a summary and asking for results stay one
   * request and one ranking.
   */
  search: (opts: {
    query: string;
    topK: number;
    sourceType?: SourceType | null;
    summarize?: boolean;
  }) => {
    const params = new URLSearchParams({ query: opts.query, top_k: String(opts.topK) });
    if (opts.sourceType) params.set("source_type", opts.sourceType);
    if (opts.summarize) params.set("summarize", "true");
    return request<SearchResponse>(`/weather/search?${params}`).then((response) => ({
      ...response,
      // Belt and braces: the API casts similarity to float8, but a Postgres
      // numeric would arrive as a string and silently break every comparison
      // and .toFixed() downstream.
      results: response.results.map((hit) => ({ ...hit, similarity: Number(hit.similarity) })),
    }));
  },

  sync: (body: { locations?: string[]; limit?: number; sources?: SourceType[] }) =>
    post<SyncResult>("/weather/sync", body),

  embed: (body: { limit?: number } = {}) => post<EmbedResult>("/weather/embed", body),

  refreshNow: (body: { limit?: number } = {}) => post<RefreshResult>("/weather/refresh", body),

  benchmark: (body: { runs?: number; top_k?: number } = {}) =>
    post<BenchmarkResult>("/weather/benchmark", body),
};
