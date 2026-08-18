import type { Feature, Sentiment } from "@/lib/types";

/**
 * Presentation for the three sentiment classes.
 *
 * Deliberately not the severity palette: severity already owns red/orange/
 * yellow on this page, and reusing it would make "negative" look like a fourth
 * severity band. These read as a separate axis — which is what they are, since
 * a forecast has a sentiment but no severity at all.
 */
export const SENTIMENT_STYLE: Record<Sentiment, { label: string; color: string; glyph: string; hint: string }> = {
  positive: {
    label: "Positive",
    color: "#4ade80",
    glyph: "▲",
    hint: "Benign or pleasant conditions",
  },
  negative: {
    label: "Negative",
    color: "#f87171",
    glyph: "▼",
    hint: "Hazardous, dangerous, or uncomfortable conditions",
  },
  neutral: {
    label: "Neutral",
    color: "#94a3b8",
    glyph: "■",
    hint: "Unremarkable conditions, or an informational bulletin",
  },
};

export const sentimentOf = (feature: Pick<Feature, "sentiment">): Sentiment =>
  feature.sentiment ?? "neutral";

/**
 * Cosine similarity as a percentage.
 *
 * The raw number, not a rescaled one. MiniLM cosines cluster in the 0.4–0.6
 * band even for good hits — a genuine top match here measures ~0.59 while a
 * deliberately irrelevant query ("recipe for bread") still returns ~0.19 — so
 * stretching the scale to make the best hit read 100% would invent a precision
 * the model does not have and hide the difference between those two cases.
 */
export const matchPercent = (similarity: number): number =>
  Math.round(Math.max(0, Math.min(1, similarity)) * 100);

/**
 * Qualitative band for a percentage, calibrated against measured scores rather
 * than round numbers: 0.19 was a nonsense query and 0.48–0.59 were good ones.
 */
export function matchBand(similarity: number): string {
  if (similarity >= 0.5) return "strong";
  if (similarity >= 0.38) return "good";
  if (similarity >= 0.25) return "weak";
  return "distant";
}
