import type { Feature } from "./types";

/**
 * The NWS severity ladder, and the one colour each rung gets everywhere -
 * globe, list, legend. Forecasts are not hazards and take the muted steel so
 * they never compete with a warning for attention.
 */
export const SEVERITY_COLORS: Record<string, string> = {
  Extreme: "#ff3b2f",
  Severe: "#ff8a00",
  Moderate: "#f5c518",
  Minor: "#3fd0c9",
  Unknown: "#7c90a6",
};

export const FORECAST_COLOR = "#5c86b8";

export function colorOf(feature: Pick<Feature, "severity" | "source_type">): string {
  if (feature.source_type === "forecast") return FORECAST_COLOR;
  return SEVERITY_COLORS[feature.severity ?? "Unknown"] ?? SEVERITY_COLORS.Unknown;
}

export const SEVERITY_ORDER = ["Extreme", "Severe", "Moderate", "Minor"];

export function severityRank(feature: Pick<Feature, "severity" | "source_type">): number {
  if (feature.source_type === "forecast") return 9;
  const index = SEVERITY_ORDER.indexOf(feature.severity ?? "");
  return index === -1 ? 8 : index;
}
