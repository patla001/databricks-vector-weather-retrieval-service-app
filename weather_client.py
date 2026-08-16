"""
Client for the National Weather Service API (api.weather.gov).

Mirrors the structure of the reference app's massive_client.py - module-level
config from the environment, one requests.Session, a generic get(), then domain
methods on top - minus the credential plumbing, because NWS needs no API key.
It does require a User-Agent that identifies the caller with a contact address;
requests without one are throttled or refused outright.

Two source types are harvested, both chosen for having genuine free-text bodies:

  * alerts    GET /alerts/active?area={ST}
              `description` + `instruction` - the long-form hazard narrative
              ("At 542 PM EDT, severe thunderstorms were located along a line...").
  * forecasts GET /gridpoints/{office}/{x},{y}/forecast
              one `detailedForecast` per period ("Scattered showers and
              thunderstorms. Partly sunny. High near 85...").

The hourly forecast endpoint is deliberately NOT used: its periods carry an
empty `detailedForecast` and only a two-word `shortForecast`, so there is no
unstructured text in it worth embedding.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("NWS_API_BASE_URL", "https://api.weather.gov")
_USER_AGENT = os.environ.get(
    "NWS_USER_AGENT",
    "(databricks-vector-weather-retrieval-service-app, epatlan1742@sdsu.edu)",
)
_DEFAULT_TIMEOUT = 30

# "state" queries every active alert in the location's state; "point" queries
# only alerts whose area covers the exact coordinate. State is the default
# because point queries routinely return nothing on a calm day, which makes for
# an empty corpus and an unconvincing demo.
DEFAULT_ALERT_SCOPE = os.environ.get("WEATHER_ALERT_SCOPE", "state")

SOURCE_ALERT = "alert"
SOURCE_FORECAST = "forecast"
VALID_SOURCES = (SOURCE_ALERT, SOURCE_FORECAST)

# NWS covers the US and its territories only, so a full geocoder would be
# overkill. This table plus the raw "lat,lon" form below covers the assignment's
# input shape ("Chicago, IL") with no third-party dependency, no rate limit, and
# no network call that can fail between the request and the grid lookup.
_CITIES: dict[str, tuple[float, float]] = {
    "albuquerque, nm": (35.0844, -106.6504),
    "anchorage, ak": (61.2181, -149.9003),
    "atlanta, ga": (33.7490, -84.3880),
    "austin, tx": (30.2672, -97.7431),
    "baltimore, md": (39.2904, -76.6122),
    "boston, ma": (42.3601, -71.0589),
    "buffalo, ny": (42.8864, -78.8784),
    "charlotte, nc": (35.2271, -80.8431),
    "chicago, il": (41.8781, -87.6298),
    "cleveland, oh": (41.4993, -81.6944),
    "dallas, tx": (32.7767, -96.7970),
    "denver, co": (39.7392, -104.9903),
    "detroit, mi": (42.3314, -83.0458),
    "honolulu, hi": (21.3069, -157.8583),
    "houston, tx": (29.7604, -95.3698),
    "indianapolis, in": (39.7684, -86.1581),
    "kansas city, mo": (39.0997, -94.5786),
    "las vegas, nv": (36.1699, -115.1398),
    "los angeles, ca": (34.0522, -118.2437),
    "memphis, tn": (35.1495, -90.0490),
    "miami, fl": (25.7617, -80.1918),
    "minneapolis, mn": (44.9778, -93.2650),
    "nashville, tn": (36.1627, -86.7816),
    "new orleans, la": (29.9511, -90.0715),
    "new york, ny": (40.7128, -74.0060),
    "oklahoma city, ok": (35.4676, -97.5164),
    "omaha, ne": (41.2565, -95.9345),
    "philadelphia, pa": (39.9526, -75.1652),
    "phoenix, az": (33.4484, -112.0740),
    "pittsburgh, pa": (40.4406, -79.9959),
    "portland, or": (45.5152, -122.6784),
    "sacramento, ca": (38.5816, -121.4944),
    "salt lake city, ut": (40.7608, -111.8910),
    "san antonio, tx": (29.4241, -98.4936),
    "san diego, ca": (32.7157, -117.1611),
    "san francisco, ca": (37.7749, -122.4194),
    "seattle, wa": (47.6062, -122.3321),
    "st. louis, mo": (38.6270, -90.1994),
    "tampa, fl": (27.9506, -82.4572),
    "washington, dc": (38.9072, -77.0369),
}

# "41.88,-87.63" / "41.88, -87.63" / "-17.5,120" - a bare coordinate pair.
_LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

# (base_url, lat, lon) -> grid point. See WeatherClient.get_point.
_POINT_CACHE: dict[tuple[str, float, float], dict] = {}


class LocationError(ValueError):
    """A location string could not be resolved to coordinates.

    Distinct from a generic ValueError so app.py can turn it into a 400 with the
    caller's message intact, while still letting real bugs reach the 500 handler.
    """


def supported_cities() -> list[str]:
    """Title-cased city names this build can resolve without coordinates."""
    return sorted(
        ", ".join(part.strip().title() if i == 0 else part.strip().upper()
                  for i, part in enumerate(key.split(",")))
        for key in _CITIES
    )


def resolve_location(raw: str) -> tuple[float, float]:
    """Resolve "City, ST" or "lat,lon" to a (latitude, longitude) pair.

    Coordinates are tried first: a caller who passes numbers means them, and a
    city table can never cover every place a user wants.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise LocationError("Location must be a non-empty string.")

    text = raw.strip()

    match = _LATLON_RE.match(text)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if not -90.0 <= lat <= 90.0:
            raise LocationError(f"Latitude out of range in {raw!r}: {lat}")
        if not -180.0 <= lon <= 180.0:
            raise LocationError(f"Longitude out of range in {raw!r}: {lon}")
        return lat, lon

    # Collapse internal whitespace so "Chicago ,  IL" matches "chicago, il".
    key = re.sub(r"\s*,\s*", ", ", " ".join(text.split())).lower()
    if key in _CITIES:
        return _CITIES[key]

    raise LocationError(
        f"Unknown location {raw!r}. Pass coordinates as \"lat,lon\" "
        f"(e.g. \"41.88,-87.63\"), or use one of the built-in cities: "
        f"{', '.join(supported_cities())}."
    )


def _text_hash(text: str) -> str:
    """Content hash of the narrative, used to detect in-place NWS revisions."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _clean(value: Any) -> str:
    """Normalize an optional API string field to a stripped str."""
    return (value or "").strip() if isinstance(value, str) else ""


class WeatherClient:
    """Thin wrapper around api.weather.gov.

    One Session per instance, so the connection to api.weather.gov is reused
    across the several calls a single /weather/sync makes.
    """

    def __init__(self, base_url: str | None = None, timeout: int = _DEFAULT_TIMEOUT):
        self.base_url = (base_url or _BASE_URL).rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                # NWS keys its rate limiting off this and will refuse a generic
                # or absent agent. It must identify the app and a contact.
                "User-Agent": _USER_AGENT,
                "Accept": "application/geo+json",
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._session.get(
            f"{self.base_url}{path}", params=params, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    # -- endpoints -----------------------------------------------------------

    def get_point(self, lat: float, lon: float) -> dict:
        """Resolve coordinates to an NWS grid point and its canonical place name.

        Cached for the process lifetime in a module-level dict rather than with
        lru_cache on the method: the cache is keyed by coordinates alone, so it
        survives the per-request WeatherClient instances app.py creates, and it
        does not pin those instances in memory the way caching on `self` would.
        Grid points are static geography - the office and grid square covering a
        coordinate do not change - so an unbounded process cache is safe.

        Coordinates are rounded to 4 decimals first because NWS redirects longer
        ones to the rounded form anyway, and rounding makes the cache hit.
        """
        key = (self.base_url, round(float(lat), 4), round(float(lon), 4))
        if key not in _POINT_CACHE:
            _POINT_CACHE[key] = self._fetch_point(key[1], key[2])
        return _POINT_CACHE[key]

    def _fetch_point(self, lat: float, lon: float) -> dict:
        props = self.get(f"/points/{lat},{lon}").get("properties", {})
        relative = (props.get("relativeLocation") or {}).get("properties") or {}
        city, state = _clean(relative.get("city")), _clean(relative.get("state"))
        return {
            "grid_id": props.get("gridId"),
            "grid_x": props.get("gridX"),
            "grid_y": props.get("gridY"),
            "forecast_url": props.get("forecast"),
            "zone_url": props.get("forecastZone"),
            "county_url": props.get("county"),
            "time_zone": props.get("timeZone"),
            "city": city,
            "state": state,
            # Canonical label. Falls back to the coordinate pair for a point in
            # the ocean or outside NWS coverage, where relativeLocation is empty.
            "location": f"{city}, {state}" if city and state else f"{lat},{lon}",
            "latitude": lat,
            "longitude": lon,
        }

    def get_active_alerts(
        self, state: str | None = None, point: tuple[float, float] | None = None
    ) -> list[dict]:
        """Active alerts for a state ("IL") or for an exact coordinate.

        Note there is no `limit` parameter: api.weather.gov rejects one on
        /alerts/active with HTTP 400 ("Query parameter \"limit\" is not
        recognized"). Callers cap the count themselves.
        """
        if point is not None:
            params = {"point": f"{round(point[0], 4)},{round(point[1], 4)}"}
        elif state:
            params = {"area": state.upper()}
        else:
            raise ValueError("get_active_alerts requires either state or point")

        return self.get("/alerts/active", params=params).get("features", []) or []

    def get_forecast_periods(self, grid_id: str, grid_x: int, grid_y: int) -> list[dict]:
        """The multi-day narrative forecast: one period per half-day."""
        data = self.get(f"/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast")
        return (data.get("properties") or {}).get("periods", []) or []

    # -- normalization -------------------------------------------------------

    @staticmethod
    def normalize_alert(feature: dict, location: str, lat: float, lon: float) -> dict | None:
        """Turn an /alerts/active GeoJSON feature into a document record.

        Returns None for an alert with no usable free text - a handful of
        administrative message types carry only structured fields, and embedding
        an empty string would put a meaningless vector in the index.
        """
        props = feature.get("properties") or {}

        alert_id = _clean(props.get("id"))
        if not alert_id:
            return None

        event = _clean(props.get("event"))
        headline = _clean(props.get("headline"))
        area_desc = _clean(props.get("areaDesc"))
        description = _clean(props.get("description"))
        instruction = _clean(props.get("instruction"))

        # The event and the affected area lead the text on purpose: queries name
        # hazards and places ("flash flood risk near rivers"), and MiniLM has no
        # access to the structured columns at retrieval time - only this string
        # becomes the vector.
        parts = [p for p in (
            f"{event} for {area_desc}." if event and area_desc else event,
            headline,
            description,
            instruction,
        ) if p]
        narrative = "\n\n".join(parts).strip()
        if not narrative:
            return None

        return {
            "id": f"alert:{alert_id}",
            "location": location,
            "latitude": lat,
            "longitude": lon,
            "source_type": SOURCE_ALERT,
            "event": event or None,
            "headline": headline or None,
            "narrative_text": narrative,
            "text_hash": _text_hash(narrative),
            "severity": _clean(props.get("severity")) or None,
            "area_desc": area_desc or None,
            "issued_at": props.get("sent"),
            "effective_at": props.get("effective") or props.get("onset"),
            "expires_at": props.get("expires") or props.get("ends"),
            "payload": feature,
        }

    @staticmethod
    def normalize_forecast_period(
        period: dict, point: dict, location: str, lat: float, lon: float
    ) -> dict | None:
        """Turn one forecast period into a document record."""
        detailed = _clean(period.get("detailedForecast"))
        if not detailed:
            # True for every period from /forecast/hourly, which is why that
            # endpoint is not used at all.
            return None

        name = _clean(period.get("name")) or f"Period {period.get('number')}"
        start = _clean(period.get("startTime"))
        grid = f"{point.get('grid_id')}/{point.get('grid_x')},{point.get('grid_y')}"

        # Keyed by grid square and period start rather than period number: the
        # numbers shift as periods roll off ("Tonight" becomes period 1), so a
        # number-keyed id would overwrite a different forecast on every sync.
        doc_id = f"forecast:{grid}:{start}"

        narrative = f"{location} - {name}: {detailed}"
        short = _clean(period.get("shortForecast"))

        return {
            "id": doc_id,
            "location": location,
            "latitude": lat,
            "longitude": lon,
            "source_type": SOURCE_FORECAST,
            "event": name,
            "headline": f"{location} - {name}: {short}" if short else f"{location} - {name}",
            "narrative_text": narrative,
            "text_hash": _text_hash(narrative),
            "severity": None,
            "area_desc": location,
            "issued_at": start or None,
            "effective_at": start or None,
            "expires_at": _clean(period.get("endTime")) or None,
            "payload": period,
        }

    # -- orchestration -------------------------------------------------------

    def fetch_documents(
        self,
        locations: Iterable[str],
        limit: int = 50,
        sources: Iterable[str] = VALID_SOURCES,
        alert_scope: str | None = None,
        log: Any = logger.info,
    ) -> tuple[list[dict], list[str]]:
        """Harvest and normalize documents for a set of locations.

        Returns (documents, errors). One location failing does not abort the
        rest - NWS returns 500s and times out often enough that an
        all-or-nothing sync would rarely complete - but the failures are handed
        back rather than swallowed, so /weather/sync can report that its count
        covers only part of what was asked for.

        `limit` caps alerts per location and forecast periods per location,
        applied client-side because api.weather.gov rejects a `limit` query
        parameter on /alerts/active (see get_active_alerts).
        """
        sources = tuple(sources)
        scope = (alert_scope or DEFAULT_ALERT_SCOPE).lower()
        documents: dict[str, dict] = {}
        errors: list[str] = []

        # Alerts are per-state, so two locations in the same state would fetch
        # the same list twice. Cache per call rather than per process - active
        # alerts change by the minute and must not be stale across syncs.
        alerts_by_state: dict[str, list[dict]] = {}

        for raw_location in locations:
            lat, lon = resolve_location(raw_location)

            try:
                point = self.get_point(lat, lon)
            except requests.RequestException as err:
                errors.append(f"{raw_location}: grid lookup failed ({err})")
                log(f"  {raw_location}: grid lookup failed, skipping ({err})")
                continue

            location = point["location"]

            if SOURCE_ALERT in sources:
                try:
                    if scope == "point":
                        features = self.get_active_alerts(point=(lat, lon))
                    else:
                        state = point.get("state")
                        if not state:
                            features = self.get_active_alerts(point=(lat, lon))
                        else:
                            if state not in alerts_by_state:
                                alerts_by_state[state] = self.get_active_alerts(state=state)
                            features = alerts_by_state[state]

                    kept = 0
                    for feature in features:
                        if kept >= limit:
                            break
                        doc = self.normalize_alert(feature, location, lat, lon)
                        if doc:
                            kept += 1
                            # An alert covering two synced locations arrives
                            # twice under one id; first write wins.
                            documents.setdefault(doc["id"], doc)
                    log(f"  {location}: {kept} alerts")
                except requests.RequestException as err:
                    errors.append(f"{location}: alerts failed ({err})")
                    log(f"  {location}: alerts failed ({err})")

            if SOURCE_FORECAST in sources:
                try:
                    periods = self.get_forecast_periods(
                        point["grid_id"], point["grid_x"], point["grid_y"]
                    )
                    kept = 0
                    for period in periods:
                        if kept >= limit:
                            break
                        doc = self.normalize_forecast_period(period, point, location, lat, lon)
                        if doc:
                            kept += 1
                            documents.setdefault(doc["id"], doc)
                    log(f"  {location}: {kept} forecast periods")
                except requests.RequestException as err:
                    errors.append(f"{location}: forecast failed ({err})")
                    log(f"  {location}: forecast failed ({err})")

        if errors:
            logger.warning("weather sync completed with %d source error(s)", len(errors))

        return list(documents.values()), errors
