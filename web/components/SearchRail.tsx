"use client";

import { useEffect, useRef } from "react";
import { colorOf } from "@/lib/severity";
import type { SearchResponse, SearchHit, SourceType } from "@/lib/types";

interface Props {
  query: string;
  onQuery: (value: string) => void;
  topK: number;
  onTopK: (value: number) => void;
  sourceType: SourceType | null;
  onSourceType: (value: SourceType | null) => void;
  summarize: boolean;
  onSummarize: (value: boolean) => void;
  /** An override runs that text instead of the box's current contents, so an
   *  example chip can search without waiting for state to settle. */
  onSubmit: (override?: string) => void;
  loading: boolean;
  error: string | null;
  response: SearchResponse | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onHover: (id: string | null) => void;
}

// Mirrors MAX_QUERY_CHARS in app.py. The query goes into the summary prompt
// verbatim, so its length is billed — stopping it at the input beats a 400.
const MAX_QUERY_CHARS = 500;

const EXAMPLES = [
  "flash flood risk this weekend",
  "dangerous heat and humidity",
  "damaging wind gusts and hail",
];

export default function SearchRail(props: Props) {
  const {
    query, onQuery, topK, onTopK, sourceType, onSourceType, summarize, onSummarize,
    onSubmit, loading, error, response, selectedId, onSelect, onHover,
  } = props;

  const listRef = useRef<HTMLDivElement>(null);

  // Keep the selected row on screen when the selection comes from the globe
  // rather than from a click in this list.
  useEffect(() => {
    if (!selectedId || !listRef.current) return;
    const row = listRef.current.querySelector<HTMLElement>(`[data-id="${CSS.escape(selectedId)}"]`);
    row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selectedId]);

  return (
    <aside className="rail">
      <div className="console">
        <form
          className="field"
          onSubmit={(event) => {
            event.preventDefault();
            onSubmit();
          }}
        >
          <input
            value={query}
            onChange={(event) => onQuery(event.target.value)}
            placeholder="Ask about the weather…"
            aria-label="Search the weather corpus"
            autoComplete="off"
            maxLength={MAX_QUERY_CHARS}
          />
        </form>

        <div className="controls">
          <button
            type="button"
            className="chip"
            data-on={sourceType === null}
            onClick={() => onSourceType(null)}
          >
            All
          </button>
          <button
            type="button"
            className="chip"
            data-on={sourceType === "alert"}
            onClick={() => onSourceType(sourceType === "alert" ? null : "alert")}
          >
            Alerts
          </button>
          <button
            type="button"
            className="chip"
            data-on={sourceType === "forecast"}
            onClick={() => onSourceType(sourceType === "forecast" ? null : "forecast")}
          >
            Forecasts
          </button>
          <button
            type="button"
            className="chip"
            data-on={summarize}
            onClick={() => onSummarize(!summarize)}
            title="Add an LLM answer grounded in the retrieved passages. Costs one model call per search; repeated questions are served from cache."
          >
            Summarize
          </button>
        </div>

        <div className="controls">
          <div className="topk">
            <label htmlFor="topk">Top K</label>
            <input
              id="topk"
              type="range"
              min={1}
              max={20}
              value={topK}
              onChange={(event) => onTopK(Number(event.target.value))}
            />
            <b className="num">{topK}</b>
          </div>
          <span className="sep" />
          <button type="button" className="btn btn-primary" onClick={() => onSubmit()} disabled={loading}>
            {loading ? <span className="spinner" /> : "Search"}
          </button>
        </div>
      </div>

      <div className="results" ref={listRef}>
        {error && <div className="err" style={{ margin: 14 }}>{error}</div>}

        {summarize && response && <SummaryCard response={response} />}

        {!response && !error && !loading && (
          <div className="empty">
            <strong>Search the corpus</strong>
            Every alert and forecast is chunked, embedded with all-MiniLM-L6-v2 and ranked by
            cosine distance in pgvector. Ask in plain language — the match is semantic, not
            keyword.
            <div style={{ marginTop: 14, display: "grid", gap: 6 }}>
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  className="chip"
                  style={{ textAlign: "left", textTransform: "none", letterSpacing: 0 }}
                  onClick={() => {
                    onQuery(example);
                    onSubmit(example);
                  }}
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        )}

        {response?.count === 0 && (
          <div className="empty">
            <strong>No matches</strong>
            {response.note ?? "Nothing in the corpus is close to that query."}
          </div>
        )}

        {response?.results.map((hit, index) => (
          <ResultRow
            key={`${hit.id}:${hit.chunk_index}`}
            hit={hit}
            rank={index + 1}
            active={hit.id === selectedId}
            onSelect={() => onSelect(hit.id)}
            onHover={onHover}
          />
        ))}
      </div>
    </aside>
  );
}

function SummaryCard({ response }: { response: SearchResponse }) {
  if (response.summary) {
    return (
      <div className="summary">
        <div className="eyebrow">
          <span>Answer</span>
        </div>
        <p>{response.summary}</p>
        <div className="model">
          Generated from the {response.count} passage{response.count === 1 ? "" : "s"} below.
        </div>
      </div>
    );
  }
  if (response.summary_error) {
    return (
      <div className="summary-off">
        <strong style={{ display: "block", marginBottom: 4 }}>Summary unavailable</strong>
        Search results are unaffected. Set <code>ANTHROPIC_API_KEY</code> on the app to enable
        the generated answer.
      </div>
    );
  }
  return null;
}

function ResultRow({
  hit,
  rank,
  active,
  onSelect,
  onHover,
}: {
  hit: SearchHit;
  rank: number;
  active: boolean;
  onSelect: () => void;
  onHover: (id: string | null) => void;
}) {
  const color = colorOf(hit);
  return (
    <button
      type="button"
      className="hit"
      data-id={hit.id}
      data-active={active}
      onClick={onSelect}
      onMouseEnter={() => onHover(hit.id)}
      onMouseLeave={() => onHover(null)}
      onFocus={() => onHover(hit.id)}
      onBlur={() => onHover(null)}
    >
      <div className="hit-top">
        <span className="hit-rank">{String(rank).padStart(2, "0")}</span>
        <span className="hit-event" style={{ color }}>
          {hit.event ?? hit.location}
        </span>
        <span className="hit-sim">{hit.similarity.toFixed(4)}</span>
      </div>
      <div className="hit-bar">
        <i style={{ width: `${Math.max(2, hit.similarity * 100)}%`, background: color }} />
      </div>
      <div className="hit-meta">
        <i className="dot" style={{ background: color }} />
        <span>{hit.location}</span>
        <span>·</span>
        <span>{hit.source_type}</span>
        {hit.geometry && (
          <>
            <span>·</span>
            <span title="Drawn from the published warning polygon">polygon</span>
          </>
        )}
      </div>
      <p className="hit-text">{hit.chunk_text}</p>
    </button>
  );
}
