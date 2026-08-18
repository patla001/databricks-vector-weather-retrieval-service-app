import * as THREE from "three";
import type { Feature, Geometry } from "./types";

export const GLOBE_RADIUS = 1;

/** Lon/lat degrees to a point on (or above) the sphere. */
export function toVector3(lon: number, lat: number, radius = GLOBE_RADIUS): THREE.Vector3 {
  const phi = (90 - lat) * (Math.PI / 180);
  const theta = (lon + 180) * (Math.PI / 180);
  return new THREE.Vector3(
    -radius * Math.sin(phi) * Math.cos(theta),
    radius * Math.cos(phi),
    radius * Math.sin(phi) * Math.sin(theta)
  );
}

/**
 * Where a feature sits on the globe.
 *
 * A polygon's centroid beats the stored latitude/longitude, which is the city
 * that was queried rather than the area the alert covers - a warning fetched
 * for "Chicago, IL" routinely draws a polygon over southern Illinois. Falling
 * back to the city point only when NWS published no polygon keeps every marker
 * as close to the truth as the data allows.
 */
export function anchorOf(feature: Feature): { lon: number; lat: number } | null {
  const centroid = polygonCentroid(feature.geometry);
  if (centroid) return centroid;
  if (feature.latitude == null || feature.longitude == null) return null;
  return { lon: feature.longitude, lat: feature.latitude };
}

export function polygonCentroid(geometry: Geometry | null): { lon: number; lat: number } | null {
  if (!geometry?.rings?.length) return null;
  let lon = 0;
  let lat = 0;
  let n = 0;
  for (const ring of geometry.rings) {
    for (const [x, y] of ring) {
      lon += x;
      lat += y;
      n += 1;
    }
  }
  return n ? { lon: lon / n, lat: lat / n } : null;
}

/** True when the anchor came from a real NWS polygon rather than the city. */
export const hasRealFootprint = (feature: Feature) => Boolean(feature.geometry?.rings?.length);

/** Plain-English provenance for a marker's position. */
export function anchorLabel(feature: Feature): string {
  if (hasRealFootprint(feature)) {
    const points = feature.geometry!.rings.reduce((n, r) => n + r.length, 0);
    return `NWS warning polygon · ${points} points`;
  }
  switch (feature.geo_source) {
    case "polygon":
      return "NWS warning polygon";
    case "zone":
      return "Centroid of the NWS zones it covers";
    case "state":
      return "State centroid — zone geometry unavailable";
    case "point":
      return feature.source_type === "forecast"
        ? "NWS forecast grid point"
        : "Requesting city — no zone geometry";
    default:
      return "Unknown";
  }
}

export interface GeoLines {
  land: [number, number][][];
  states: [number, number][][];
  nation: [number, number][][];
}
