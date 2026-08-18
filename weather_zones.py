"""
Geographic anchors for weather documents.

Most active NWS alerts carry no inline polygon: on a sample of 189 nationwide
alerts only 38 (20%) had a `geometry`, because the rest are issued against
*zones* (county or marine areas) and reference them by URL in `affectedZones`.
Before this module existed, every such alert inherited the coordinates of
whichever city happened to request it, so a statewide Illinois advisory was
plotted on top of Chicago and the globe showed one dot per synced city rather
than one per hazard.

Three tiers, best first:

  1. the alert's own `geometry` centroid            - exact, free, no network
  2. the centroid of its `affectedZones`            - accurate, network + cached
  3. the centroid of the state in its UGC codes     - coarse, free, always works

Tier 2 is what needs care. Zone polygons are ~26 KB each and there are ~3600 of
them nationally, far too many to fetch per run - but zones are *static
geography*, so a centroid fetched once is good forever. This module keeps them
in a small table (one row, two floats per zone) and fetches only what it has
never seen, under a per-run budget so a cold cache degrades to tier 3 instead of
turning a five-minute job into an hour-long one.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, Sequence

import lakebase

logger = logging.getLogger(__name__)

DEFAULT_ZONES_TABLE = os.environ.get("WEATHER_ZONES_TABLE", "weather_zones")

# How many never-before-seen zones one run may fetch. A cold cache needs ~250
# fetches to cover a typical day's alerts at ZONE_SAMPLE=3; the default clears
# that in a single run while still bounding the worst case (~0.5s per fetch).
ZONE_FETCH_BUDGET = int(os.environ.get("WEATHER_ZONE_FETCH_BUDGET", "400"))

# Zones sampled per alert. An alert can list 40+ counties, but a map marker only
# needs a representative point: sampling 3 cuts the distinct-zone count for a
# nationwide harvest from 1022 to 253 at no visible cost to placement.
ZONE_SAMPLE = int(os.environ.get("WEATHER_ZONE_SAMPLE", "3"))

# Geographic center of each state, verified against api.weather.gov/points -
# every coordinate below resolves to the state it is keyed under. Used only when
# a zone centroid is unavailable, which after the first run means a brand-new
# zone id. Marine areas (PZ, AM, GM, LM, PK...) are deliberately absent: they
# are not states, and guessing a land centroid for an offshore warning would put
# it somewhere it demonstrably is not. Those fall through to "no anchor".
STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AL": (32.806, -86.791), "AK": (63.588, -154.493), "AZ": (34.048, -111.094),
    "AR": (34.800, -92.199), "CA": (36.778, -119.418), "CO": (39.114, -105.358),
    "CT": (41.603, -73.088), "DE": (38.911, -75.528), "DC": (38.9072, -77.0369),
    "FL": (27.766, -81.687), "GA": (33.040, -83.643), "HI": (20.798, -156.331),
    "ID": (44.068, -114.742), "IL": (40.349, -88.986), "IN": (39.849, -86.258),
    "IA": (42.011, -93.210), "KS": (38.526, -96.726), "KY": (37.668, -84.670),
    "LA": (31.169, -91.868), "ME": (44.694, -69.381), "MD": (39.064, -76.802),
    "MA": (42.230, -71.530), "MI": (43.326, -84.536), "MN": (45.694, -93.900),
    "MS": (32.741, -89.678), "MO": (38.456, -92.288), "MT": (46.921, -110.454),
    "NE": (41.125, -98.268), "NV": (38.313, -117.055), "NH": (43.452, -71.563),
    "NJ": (40.298, -74.521), "NM": (34.840, -106.248), "NY": (42.165, -74.948),
    "NC": (35.630, -79.806), "ND": (47.528, -99.784), "OH": (40.388, -82.764),
    "OK": (35.565, -96.928), "OR": (44.572, -122.070), "PA": (40.590, -77.209),
    "RI": (41.680, -71.511), "SC": (33.856, -80.945), "SD": (44.299, -99.438),
    "TN": (35.747, -86.692), "TX": (31.054, -97.563), "UT": (40.150, -111.862),
    "VT": (44.045, -72.710), "VA": (37.769, -78.170), "WA": (47.400, -121.490),
    "WV": (38.491, -80.954), "WI": (44.268, -89.616), "WY": (42.756, -107.302),
    "PR": (18.220, -66.590),
}

# "https://api.weather.gov/zones/forecast/OKZ074" -> ("forecast", "OKZ074")
_ZONE_URL_RE = re.compile(r"/zones/([a-z]+)/([A-Z]{2}[A-Z]\d{3})\b")

# Anchor provenance, stored on the document so the UI can say how it knows.
GEO_POLYGON = "polygon"
GEO_ZONE = "zone"
GEO_STATE = "state"
GEO_POINT = "point"


def coordinates_of(geometry: Any) -> tuple[float, float] | None:
    """Mean vertex of a GeoJSON geometry, as (latitude, longitude).

    A vertex mean, not a true area centroid: for the near-convex blobs NWS
    publishes the two land within a few km of each other, and the vertex mean
    cannot fall outside the hull the way an area centroid can for a crescent.
    """
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    if coords is None:
        return None

    total_lon = total_lat = 0.0
    count = 0

    def walk(node: Any) -> None:
        nonlocal total_lon, total_lat, count
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and isinstance(node[0], (int, float))
            and isinstance(node[1], (int, float))
        ):
            total_lon += float(node[0])
            total_lat += float(node[1])
            count += 1
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(coords)
    if not count:
        return None
    return (round(total_lat / count, 4), round(total_lon / count, 4))


def state_of(zone_id: str) -> str | None:
    """State postal code embedded in a UGC/zone id, e.g. "OKZ074" -> "OK"."""
    if isinstance(zone_id, str) and len(zone_id) >= 2:
        code = zone_id[:2].upper()
        if code in STATE_CENTROIDS:
            return code
    return None


def state_centroid(codes: Iterable[str]) -> tuple[float, float] | None:
    """Mean centroid of every recognizable state among some UGC/zone ids."""
    points = []
    for code in codes or ():
        state = state_of(code)
        if state:
            points.append(STATE_CENTROIDS[state])
    if not points:
        return None
    return (
        round(sum(p[0] for p in points) / len(points), 4),
        round(sum(p[1] for p in points) / len(points), 4),
    )


def ensure_zones_table(table: str = DEFAULT_ZONES_TABLE) -> None:
    """Create the zone-centroid cache if it is not there yet.

    Safe for the app role to run: no extension, no vector, no privileged DDL.
    """
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            zone_id    TEXT PRIMARY KEY,
            zone_type  TEXT,
            name       TEXT,
            state      TEXT,
            latitude   DOUBLE PRECISION,
            longitude  DOUBLE PRECISION,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


class ZoneCentroids:
    """Resolves zone URLs to a single anchor point, cached in Postgres.

    Constructed per sync. The in-process memo lives for the run; the table
    outlives it, so the network cost falls to zero once the cache is warm.

    Passing `persist=False` keeps everything in memory, which is what the tests
    and any DB-less caller want.
    """

    def __init__(
        self,
        client: Any,
        table: str = DEFAULT_ZONES_TABLE,
        budget: int | None = None,
        persist: bool = True,
        log: Any = logger.info,
    ):
        self._client = client
        self._table = table
        self._budget = ZONE_FETCH_BUDGET if budget is None else budget
        self._persist = persist
        self._log = log
        self._memo: dict[str, tuple[float, float] | None] = {}
        self._primed = False
        self.fetched = 0
        self.failed = 0
        self.budget_exhausted = False

    # -- cache ---------------------------------------------------------------

    def _prime(self) -> None:
        """Load every known centroid in one query.

        The whole table is a few thousand rows of two floats, so reading it
        wholesale beats a per-zone SELECT by a wide margin and makes the common
        case - a warm cache - exactly one round trip.
        """
        if self._primed:
            return
        self._primed = True
        if not self._persist:
            return
        try:
            ensure_zones_table(self._table)
            rows = lakebase.run_query(
                f"SELECT zone_id, latitude, longitude FROM {self._table} "
                f"WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
            )
        except Exception as err:  # noqa: BLE001 - cache is optional, never fatal
            self._log(f"  zone cache unavailable, resolving live ({err})")
            self._persist = False
            return
        for row in rows:
            self._memo[row["zone_id"]] = (float(row["latitude"]), float(row["longitude"]))
        self._log(f"  zone cache: {len(self._memo)} centroid(s) loaded")

    def _store(self, records: Sequence[dict]) -> None:
        if not records or not self._persist:
            return
        try:
            rows = [
                (r["zone_id"], r.get("zone_type"), r.get("name"), r.get("state"),
                 r["latitude"], r["longitude"])
                for r in records
            ]
            with lakebase.get_connection() as conn:
                with conn.cursor() as cur:
                    lakebase.execute_values(
                        cur,
                        f"""
                        INSERT INTO {self._table}
                            (zone_id, zone_type, name, state, latitude, longitude)
                        VALUES %s
                        ON CONFLICT (zone_id) DO NOTHING
                        """,
                        rows,
                        template="(%s,%s,%s,%s,%s,%s)",
                    )
                conn.commit()
        except Exception as err:  # noqa: BLE001
            self._log(f"  zone cache write failed, continuing ({err})")

    # -- resolution ----------------------------------------------------------

    def _fetch(self, url: str, zone_id: str) -> dict | None:
        """One zone polygon -> a centroid record, or None if it cannot be had."""
        try:
            payload = self._client.get_absolute(url)
        except Exception as err:  # noqa: BLE001 - a dead zone must not kill the sync
            self.failed += 1
            logger.debug("zone %s failed: %s", zone_id, err)
            return None

        point = coordinates_of(payload.get("geometry"))
        if not point:
            self.failed += 1
            return None

        props = payload.get("properties") or {}
        match = _ZONE_URL_RE.search(url)
        return {
            "zone_id": zone_id,
            "zone_type": match.group(1) if match else None,
            "name": props.get("name"),
            "state": props.get("state") or state_of(zone_id),
            "latitude": point[0],
            "longitude": point[1],
        }

    def resolve(self, zone_urls: Sequence[str]) -> tuple[float, float] | None:
        """Anchor for a set of affectedZones URLs, or None if none resolve."""
        if not zone_urls:
            return None
        self._prime()

        wanted: list[tuple[str, str]] = []
        for url in zone_urls[:ZONE_SAMPLE]:
            match = _ZONE_URL_RE.search(url or "")
            if match:
                wanted.append((match.group(2), url))

        fresh: list[dict] = []
        points: list[tuple[float, float]] = []
        for zone_id, url in wanted:
            if zone_id in self._memo:
                cached = self._memo[zone_id]
                if cached:
                    points.append(cached)
                continue
            if self.fetched >= self._budget:
                self.budget_exhausted = True
                continue
            self.fetched += 1
            record = self._fetch(url, zone_id)
            if record:
                point = (record["latitude"], record["longitude"])
                self._memo[zone_id] = point
                points.append(point)
                fresh.append(record)
            else:
                # Remember the miss so a broken zone is not retried all run.
                self._memo[zone_id] = None

        self._store(fresh)

        if not points:
            return None
        return (
            round(sum(p[0] for p in points) / len(points), 4),
            round(sum(p[1] for p in points) / len(points), 4),
        )

    def stats(self) -> dict:
        return {
            "cached": sum(1 for v in self._memo.values() if v),
            "fetched": self.fetched,
            "failed": self.failed,
            "budget_exhausted": self.budget_exhausted,
        }
