/**
 * Turn the TopoJSON atlases into the flat polyline arrays the globe draws.
 *
 * Runs at build time so the browser never loads topojson-client or the full
 * atlases: the globe only ever needs outlines, and decoding arcs at runtime
 * would ship a decoder plus several times the data to produce the same lines.
 *
 * World coastlines give the sphere its shape; US state borders are the detail
 * layer, because every document in this corpus is a US National Weather
 * Service product and the state is how NWS scopes an alert.
 */
import { writeFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import * as topojson from "topojson-client";

const require = createRequire(import.meta.url);

// Coordinates are drawn on a sphere of radius 1 in a viewport ~1000px wide, so
// a degree is at most ~3px. Two decimals is ~1km - already finer than a pixel.
const PRECISION = 2;

function ringsFrom(geojson, { minStep = 0, minExtent = 0 } = {}) {
  const out = [];
  const push = (coords) => {
    const line = [];
    for (const [lon, lat] of coords) {
      const p = [Number(lon.toFixed(PRECISION)), Number(lat.toFixed(PRECISION))];
      const prev = line[line.length - 1];
      if (prev) {
        // Drop points closer than minStep degrees to the last one kept. The
        // 10m atlases carry far more detail than a globe at this scale can
        // show; the raw national outline is 8,800 points for a shape that
        // reads identically at a few hundred.
        const dx = (p[0] - prev[0]) * Math.cos((p[1] * Math.PI) / 180);
        const dy = p[1] - prev[1];
        if (Math.hypot(dx, dy) < minStep) continue;
      }
      line.push(p);
    }
    if (line.length < 2) return;
    // Discard islands too small to register - each is a handful of points that
    // renders as a single speck.
    if (minExtent > 0) {
      const xs = line.map((q) => q[0]);
      const ys = line.map((q) => q[1]);
      const extent = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
      if (extent < minExtent) return;
    }
    out.push(line);
  };
  const walk = (geom) => {
    if (!geom) return;
    switch (geom.type) {
      case "Polygon":
        geom.coordinates.forEach(push);
        break;
      case "MultiPolygon":
        geom.coordinates.forEach((poly) => poly.forEach(push));
        break;
      case "LineString":
        push(geom.coordinates);
        break;
      case "MultiLineString":
        geom.coordinates.forEach(push);
        break;
      case "GeometryCollection":
        geom.geometries.forEach(walk);
        break;
    }
  };
  (geojson.features ?? [geojson]).forEach((f) => walk(f.geometry ?? f));
  return out;
}

const world = require("world-atlas/land-110m.json");
const land = ringsFrom(topojson.feature(world, world.objects.land), {
  minStep: 0.35,
  minExtent: 1.5,
});

// mesh() rather than feature(): it returns each shared border once instead of
// once per adjacent state, which halves the line count and stops seams from
// drawing at double brightness.
const us = require("us-atlas/states-10m.json");
const states = ringsFrom(topojson.mesh(us, us.objects.states, (a, b) => a !== b), {
  minStep: 0.12,
});
const nation = ringsFrom(topojson.mesh(us, us.objects.nation), {
  minStep: 0.12,
  minExtent: 1.0,
});

const payload = { land, states, nation };
mkdirSync("public", { recursive: true });
writeFileSync("public/geo.json", JSON.stringify(payload));

const count = (r) => r.reduce((n, l) => n + l.length, 0);
console.log(
  `geo.json  land ${land.length} lines/${count(land)} pts  ` +
    `states ${states.length}/${count(states)}  nation ${nation.length}/${count(nation)}  ` +
    `= ${(JSON.stringify(payload).length / 1024).toFixed(0)} KiB`
);
