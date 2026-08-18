"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useState } from "react";
import DetailPanel from "@/components/DetailPanel";
import PipelinePanel from "@/components/PipelinePanel";
import SearchRail from "@/components/SearchRail";
import { api, ApiError } from "@/lib/api";
import { FORECAST_COLOR, SEVERITY_COLORS } from "@/lib/severity";
import { SENTIMENT_STYLE, sentimentOf } from "@/lib/sentiment";
import type {
  Feature, MapResponse, RefreshStatus, SearchResponse, Sentiment, SourceType, Stats,
} from "@/lib/types";

// WebGL has no server-side equivalent, and this page is statically exported, so
// the globe must not be part of the prerendered HTML.
const Globe = dynamic(() => import("@/components/Globe"), {
  ssr: false,
  loading: () => <div className="globe-layer" />,
});

export default function Page() {
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(8);
  const [sourceType, setSourceType] = useState<SourceType | null>(null);
  // Off by default: every summarized search is a model call, and most
  // searches only want the ranked list.
  const [summarize, setSummarize] = useState(false);

  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [response, setResponse] = useState<SearchResponse | null>(null);

  const [map, setMap] = useState<MapResponse | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [refresh, setRefresh] = useState<RefreshStatus | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [detail, setDetail] = useState<(Feature & { narrative_text?: string }) | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [pipelineOpen, setPipelineOpen] = useState(false);
  const [includeExpired, setIncludeExpired] = useState(false);
  // Null means "show everything". Clicking a legend chip isolates that class,
  // clicking it again clears the filter.
  const [sentimentFilter, setSentimentFilter] = useState<Sentiment | null>(null);

  const loadCorpus = useCallback(async () => {
    try {
      const [mapResponse, statsResponse, refreshResponse] = await Promise.all([
        api.map({ includeExpired }),
        api.stats(),
        api.refreshStatus(),
      ]);
      setMap(mapResponse);
      setStats(statsResponse);
      setRefresh(refreshResponse);
      setBootError(null);
    } catch (err) {
      setBootError(err instanceof ApiError ? err.message : String(err));
    }
  }, [includeExpired]);

  useEffect(() => {
    void loadCorpus();
  }, [loadCorpus]);

  // The scheduler runs every 30 minutes; polling the cheap status endpoint keeps
  // the header honest about freshness without reloading the map.
  useEffect(() => {
    const timer = setInterval(() => {
      api.refreshStatus().then(setRefresh).catch(() => {});
    }, 60_000);
    return () => clearInterval(timer);
  }, []);

  const runSearch = useCallback(
    async (override?: string) => {
      const text = (override ?? query).trim();
      if (!text) return;
      setSearching(true);
      setSearchError(null);
      setSelectedId(null);
      try {
        setResponse(await api.search({ query: text, topK, sourceType, summarize }));
      } catch (err) {
        setResponse(null);
        setSearchError(err instanceof ApiError ? err.message : String(err));
      } finally {
        setSearching(false);
      }
    },
    [query, topK, sourceType, summarize]
  );

  // Open the full document for whatever is selected. Search hits already carry
  // the matched chunk, but not the whole narrative, which the map view omits by
  // design - so the body is fetched on demand rather than for all 450 rows.
  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    api
      .document(selectedId)
      .then((doc) => {
        if (!cancelled) setDetail(doc);
      })
      .catch(() => {
        if (!cancelled) {
          setDetail(map?.features.find((f) => f.id === selectedId) ?? null);
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, map]);

  const hits = response?.results ?? [];

  // Search hits are merged into the map layer so a result that has expired, or
  // that the map's limit cut off, still gets a marker to fly to.
  const features = useMemo(() => {
    const merged = new Map<string, Feature>();
    for (const feature of map?.features ?? []) merged.set(feature.id, feature);
    for (const hit of hits) if (!merged.has(hit.id)) merged.set(hit.id, hit);
    const all = [...merged.values()];
    if (!sentimentFilter) return all;
    // Search hits are never filtered out: hiding a result the list is still
    // showing would break the link between the two halves of the page.
    const hitIds = new Set(hits.map((h) => h.id));
    return all.filter((f) => hitIds.has(f.id) || sentimentOf(f) === sentimentFilter);
  }, [map, hits, sentimentFilter]);

  const sentimentCounts = useMemo(() => {
    const counts: Record<Sentiment, number> = { positive: 0, negative: 0, neutral: 0 };
    for (const feature of map?.features ?? []) counts[sentimentOf(feature)] += 1;
    return counts;
  }, [map]);

  const selectedHit = hits.find((hit) => hit.id === selectedId) ?? null;
  const lastRun = refresh?.last_finished_at ? new Date(refresh.last_finished_at) : null;

  return (
    <div className="shell">
      <Globe
        features={features}
        hits={hits}
        selectedId={selectedId}
        hoveredId={hoveredId}
        onSelect={setSelectedId}
        onHover={setHoveredId}
      />

      <header className="topbar">
        <div className="brand">
          <h1>Weather Intelligence</h1>
          <span className="src">NWS → Lakebase pgvector → semantic search</span>
        </div>

        <div className="vitals">
          <div className="vital">
            <b>{stats?.documents ?? "—"}</b>
            <span>Documents</span>
          </div>
          <div className="vital">
            <b>{stats?.embeddings ?? "—"}</b>
            <span>Vectors</span>
          </div>
          <div className="vital" data-warn={Boolean(stats?.pending)}>
            <b>{stats?.pending ?? "—"}</b>
            <span>Pending</span>
          </div>
          <div className="vital">
            <b>{map?.count ?? "—"}</b>
            <span>{includeExpired ? "Plotted" : "Active"}</span>
          </div>
          <div
            className="vital"
            title={
              refresh?.enabled
                ? `Refreshes every ${refresh.interval_minutes} min · ${refresh.cycles} cycles run`
                : "The in-app scheduler is disabled"
            }
            style={{ flexDirection: "row", alignItems: "center", gap: 7 }}
          >
            <span className="pulse" data-off={!refresh?.enabled} />
            <div style={{ display: "flex", flexDirection: "column" }}>
              <b style={{ fontSize: 12 }}>
                {lastRun
                  ? lastRun.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
                  : refresh?.enabled
                    ? "queued"
                    : "off"}
              </b>
              <span>Refreshed</span>
            </div>
          </div>

          <button
            type="button"
            className="chip"
            data-on={includeExpired}
            onClick={() => setIncludeExpired((value) => !value)}
            title="Active alerts expire within hours. Include the expired ones to see the whole corpus."
          >
            Expired
          </button>
          <button
            type="button"
            className="chip"
            data-on={pipelineOpen}
            onClick={() => setPipelineOpen((value) => !value)}
          >
            Pipeline
          </button>
        </div>
      </header>

      <SearchRail
        query={query}
        onQuery={setQuery}
        topK={topK}
        onTopK={setTopK}
        sourceType={sourceType}
        onSourceType={setSourceType}
        summarize={summarize}
        onSummarize={setSummarize}
        onSubmit={runSearch}
        loading={searching}
        error={searchError ?? bootError}
        response={response}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onHover={setHoveredId}
      />

      <DetailPanel
        feature={detail}
        hit={selectedHit}
        loading={detailLoading}
        onClose={() => setSelectedId(null)}
      />

      {pipelineOpen && (
        <PipelinePanel
          stats={stats}
          refresh={refresh}
          onClose={() => setPipelineOpen(false)}
          onChanged={loadCorpus}
        />
      )}

      <div className="legend">
        {Object.entries(SEVERITY_COLORS)
          .filter(([name]) => name !== "Unknown")
          .map(([name, color]) => (
            <div className="legend-item" key={name}>
              <i style={{ background: color }} />
              <span>{name}</span>
            </div>
          ))}
        <div className="legend-item">
          <i style={{ background: FORECAST_COLOR }} />
          <span>Forecast</span>
        </div>

        <span className="legend-rule" />

        {(["negative", "neutral", "positive"] as Sentiment[]).map((tone) => (
          <button
            type="button"
            key={tone}
            className="legend-item legend-filter"
            data-on={sentimentFilter === tone}
            title={`${SENTIMENT_STYLE[tone].hint} — ${sentimentCounts[tone]} on the globe. Click to isolate.`}
            onClick={() => setSentimentFilter(sentimentFilter === tone ? null : tone)}
          >
            <i style={{ background: SENTIMENT_STYLE[tone].color }} />
            <span>{SENTIMENT_STYLE[tone].label}</span>
            <b>{sentimentCounts[tone]}</b>
          </button>
        ))}
      </div>

      <div className="hud-hint">
        {sentimentFilter
          ? `showing ${sentimentFilter} only · click the chip again to clear`
          : "drag to rotate · scroll to zoom"}
        <br />
        bar height = cosine similarity
      </div>
    </div>
  );
}
