"use client";

import { colorOf } from "@/lib/severity";
import { anchorLabel } from "@/lib/geo";
import { SENTIMENT_STYLE, matchBand, matchPercent, sentimentOf } from "@/lib/sentiment";
import type { Feature, SearchHit } from "@/lib/types";

interface Props {
  feature: (Feature & { narrative_text?: string }) | null;
  hit: SearchHit | null;
  loading: boolean;
  onClose: () => void;
}

function when(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}

export default function DetailPanel({ feature, hit, loading, onClose }: Props) {
  if (!feature && !loading) return null;
  const color = feature ? colorOf(feature) : "var(--muted)";
  const expired = feature?.expires_at ? new Date(feature.expires_at) < new Date() : false;

  return (
    <aside className="detail">
      <div className="detail-head">
        <div style={{ flex: 1 }}>
          <div className="eyebrow" style={{ color }}>
            {feature?.source_type === "forecast" ? "Forecast" : feature?.severity ?? "Alert"}
            {expired ? " · expired" : ""}
          </div>
          <h2>{feature?.event ?? (loading ? "Loading…" : "")}</h2>
        </div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close details">
          ×
        </button>
      </div>

      <div className="detail-body">
        {loading && !feature && <p style={{ color: "var(--muted)" }}>Loading document…</p>}

        {feature && (
          <>
            {hit && (
              <div className="matched">
                <div className="eyebrow" style={{ color: "var(--minor)", marginBottom: 6 }}>
                  Matched passage · {matchPercent(hit.similarity)}% match
                  {" · "}{matchBand(hit.similarity)} · cosine {hit.similarity.toFixed(4)}
                </div>
                {hit.chunk_text}
              </div>
            )}

            <dl className="kv">
              <dt>Location</dt>
              <dd>{feature.location}</dd>
              {feature.area_desc && (
                <>
                  <dt>Area</dt>
                  <dd>{feature.area_desc}</dd>
                </>
              )}
              <dt>Issued</dt>
              <dd>{when(feature.issued_at)}</dd>
              <dt>Expires</dt>
              <dd style={{ color: expired ? "var(--severe)" : undefined }}>
                {when(feature.expires_at)}
              </dd>
              <dt>Outlook</dt>
              <dd>
                <span className="tag-sentiment" data-sentiment={sentimentOf(feature)}>
                  <i aria-hidden="true">{SENTIMENT_STYLE[sentimentOf(feature)].glyph}</i>
                  {SENTIMENT_STYLE[sentimentOf(feature)].label}
                </span>
                <span style={{ color: "var(--faint)", marginLeft: 8, fontSize: 11 }}>
                  {SENTIMENT_STYLE[sentimentOf(feature)].hint}
                </span>
              </dd>
              <dt>Footprint</dt>
              <dd>{anchorLabel(feature)}</dd>
              <dt>Coords</dt>
              <dd>
                {feature.latitude?.toFixed(4)}, {feature.longitude?.toFixed(4)}
              </dd>
              <dt>Id</dt>
              <dd style={{ fontSize: 10, color: "var(--faint)" }}>{feature.id}</dd>
            </dl>

            {feature.headline && (
              <p style={{ fontSize: 13, lineHeight: 1.6, color: "var(--paper)", marginTop: 0 }}>
                {feature.headline}
              </p>
            )}

            {feature.narrative_text && (
              <div className="narrative">{feature.narrative_text}</div>
            )}
          </>
        )}
      </div>
    </aside>
  );
}
