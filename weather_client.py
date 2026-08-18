"""
Client for the National Weather Service API (api.weather.gov).

Mirrors the structure of the reference app's massive_client.py - module-level
config from the environment, one requests.Session, a generic get(), then domain
methods on top - minus the credential plumbing, because NWS needs no API key.
It does require a User-Agent that identifies the caller with a contact address;
requests without one are throttled or refused outright.

Two source types are harvested, both chosen for having genuine free-text bodies:

  * alerts    GET /alerts/active  (nationwide) or ?area={ST}
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

import weather_zones

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("NWS_API_BASE_URL", "https://api.weather.gov")
_USER_AGENT = os.environ.get(
    "NWS_USER_AGENT",
    "(databricks-vector-weather-retrieval-service-app, epatlan1742@sdsu.edu)",
)
_DEFAULT_TIMEOUT = 30

# How much of the alert feed to take:
#   "national" - every active alert in the country, in a single request
#   "state"    - only alerts in the states covered by the requested locations
#   "point"    - only alerts whose area covers an exact coordinate
#
# National is the default. It is *cheaper* than the alternatives, not more
# expensive: /alerts/active with no area parameter returns the whole country in
# one call, where "state" costs one call per distinct state and still leaves the
# map blank everywhere the caller did not think to ask about. Point queries
# routinely return nothing on a calm day.
DEFAULT_ALERT_SCOPE = os.environ.get("WEATHER_ALERT_SCOPE", "national")
VALID_ALERT_SCOPES = ("national", "state", "point")

# Ceiling on a single nationwide alert harvest. Active alerts nationwide run
# ~200 on a calm day and into the low thousands during a major outbreak; this
# is a runaway guard, not a sampling target.
NATIONAL_ALERT_LIMIT = int(os.environ.get("WEATHER_NATIONAL_ALERT_LIMIT", "2000"))

SOURCE_ALERT = "alert"
SOURCE_FORECAST = "forecast"
VALID_SOURCES = (SOURCE_ALERT, SOURCE_FORECAST)

# NWS covers the US and its territories only, so a full geocoder would be
# overkill. This table plus the raw "lat,lon" form below covers the assignment's
# input shape ("Chicago, IL") with no third-party dependency, no rate limit, and
# no network call that can fail between the request and the grid lookup.
_CITIES: dict[str, tuple[float, float]] = {
    "anchorage, ak": (61.2181, -149.9003),
    "fairbanks, ak": (64.8378, -147.7164),
    "juneau, ak": (58.3019, -134.4197),
    "birmingham, al": (33.5186, -86.8104),
    "huntsville, al": (34.7304, -86.5861),
    "mobile, al": (30.6954, -88.0399),
    "montgomery, al": (32.3668, -86.3000),
    "fayetteville, ar": (36.0626, -94.1574),
    "jonesboro, ar": (35.8423, -90.7043),
    "little rock, ar": (34.7465, -92.2896),
    "flagstaff, az": (35.1983, -111.6513),
    "phoenix, az": (33.4484, -112.0740),
    "tucson, az": (32.2226, -110.9747),
    "fresno, ca": (36.7378, -119.7871),
    "los angeles, ca": (34.0522, -118.2437),
    "redding, ca": (40.5865, -122.3917),
    "sacramento, ca": (38.5816, -121.4944),
    "san diego, ca": (32.7157, -117.1611),
    "san francisco, ca": (37.7749, -122.4194),
    "colorado springs, co": (38.8339, -104.8214),
    "denver, co": (39.7392, -104.9903),
    "grand junction, co": (39.0639, -108.5506),
    "pueblo, co": (38.2544, -104.6091),
    "bridgeport, ct": (41.1865, -73.1952),
    "hartford, ct": (41.7658, -72.6734),
    "new haven, ct": (41.3083, -72.9279),
    "washington, dc": (38.9072, -77.0369),
    "dover, de": (39.1582, -75.5244),
    "wilmington, de": (39.7391, -75.5398),
    "jacksonville, fl": (30.3322, -81.6557),
    "key west, fl": (24.5551, -81.7800),
    "miami, fl": (25.7617, -80.1918),
    "orlando, fl": (28.5383, -81.3792),
    "tallahassee, fl": (30.4383, -84.2807),
    "tampa, fl": (27.9506, -82.4572),
    "atlanta, ga": (33.7490, -84.3880),
    "augusta, ga": (33.4735, -82.0105),
    "columbus, ga": (32.4610, -84.9877),
    "savannah, ga": (32.0809, -81.0912),
    "hilo, hi": (19.7297, -155.0900),
    "honolulu, hi": (21.3069, -157.8583),
    "kahului, hi": (20.8893, -156.4729),
    "cedar rapids, ia": (41.9779, -91.6656),
    "des moines, ia": (41.5868, -93.6250),
    "sioux city, ia": (42.4963, -96.4049),
    "boise, id": (43.6150, -116.2023),
    "coeur d'alene, id": (47.6777, -116.7805),
    "idaho falls, id": (43.4917, -112.0339),
    "chicago, il": (41.8781, -87.6298),
    "peoria, il": (40.6936, -89.5890),
    "rockford, il": (42.2711, -89.0940),
    "springfield, il": (39.7817, -89.6501),
    "evansville, in": (37.9716, -87.5711),
    "fort wayne, in": (41.0793, -85.1394),
    "indianapolis, in": (39.7684, -86.1581),
    "south bend, in": (41.6764, -86.2520),
    "dodge city, ks": (37.7528, -100.0171),
    "topeka, ks": (39.0473, -95.6752),
    "wichita, ks": (37.6872, -97.3301),
    "bowling green, ky": (36.9685, -86.4808),
    "lexington, ky": (38.0406, -84.5037),
    "louisville, ky": (38.2527, -85.7585),
    "baton rouge, la": (30.4515, -91.1871),
    "lake charles, la": (30.2266, -93.2174),
    "new orleans, la": (29.9511, -90.0715),
    "shreveport, la": (32.5252, -93.7502),
    "boston, ma": (42.3601, -71.0589),
    "springfield, ma": (42.1015, -72.5898),
    "worcester, ma": (42.2626, -71.8023),
    "annapolis, md": (38.9784, -76.4922),
    "baltimore, md": (39.2904, -76.6122),
    "hagerstown, md": (39.6418, -77.7200),
    "bangor, me": (44.8016, -68.7712),
    "caribou, me": (46.8606, -68.0111),
    "portland, me": (43.6591, -70.2568),
    "detroit, mi": (42.3314, -83.0458),
    "grand rapids, mi": (42.9634, -85.6681),
    "marquette, mi": (46.5436, -87.3954),
    "traverse city, mi": (44.7631, -85.6206),
    "duluth, mn": (46.7867, -92.1005),
    "minneapolis, mn": (44.9778, -93.2650),
    "rochester, mn": (44.0121, -92.4802),
    "kansas city, mo": (39.0997, -94.5786),
    "springfield, mo": (37.2090, -93.2923),
    "st. louis, mo": (38.6270, -90.1994),
    "gulfport, ms": (30.3674, -89.0928),
    "jackson, ms": (32.2988, -90.1848),
    "tupelo, ms": (34.2576, -88.7034),
    "billings, mt": (45.7833, -108.5007),
    "great falls, mt": (47.5053, -111.3008),
    "missoula, mt": (46.8721, -113.9940),
    "asheville, nc": (35.5951, -82.5515),
    "charlotte, nc": (35.2271, -80.8431),
    "raleigh, nc": (35.7796, -78.6382),
    "wilmington, nc": (34.2257, -77.9447),
    "bismarck, nd": (46.8083, -100.7837),
    "fargo, nd": (46.8772, -96.7898),
    "minot, nd": (48.2330, -101.2963),
    "lincoln, ne": (40.8136, -96.7026),
    "north platte, ne": (41.1239, -100.7654),
    "omaha, ne": (41.2565, -95.9345),
    "concord, nh": (43.2081, -71.5376),
    "manchester, nh": (42.9956, -71.4548),
    "atlantic city, nj": (39.3643, -74.4229),
    "newark, nj": (40.7357, -74.1724),
    "trenton, nj": (40.2171, -74.7429),
    "albuquerque, nm": (35.0844, -106.6504),
    "las cruces, nm": (32.3199, -106.7637),
    "santa fe, nm": (35.6870, -105.9378),
    "elko, nv": (40.8324, -115.7631),
    "las vegas, nv": (36.1699, -115.1398),
    "reno, nv": (39.5296, -119.8138),
    "albany, ny": (42.6526, -73.7562),
    "buffalo, ny": (42.8864, -78.8784),
    "new york, ny": (40.7128, -74.0060),
    "syracuse, ny": (43.0481, -76.1474),
    "cincinnati, oh": (39.1031, -84.5120),
    "cleveland, oh": (41.4993, -81.6944),
    "columbus, oh": (39.9612, -82.9988),
    "toledo, oh": (41.6528, -83.5379),
    "lawton, ok": (34.6036, -98.3959),
    "oklahoma city, ok": (35.4676, -97.5164),
    "tulsa, ok": (36.1540, -95.9928),
    "bend, or": (44.0582, -121.3153),
    "eugene, or": (44.0521, -123.0868),
    "medford, or": (42.3265, -122.8756),
    "portland, or": (45.5152, -122.6784),
    "erie, pa": (42.1292, -80.0851),
    "harrisburg, pa": (40.2732, -76.8867),
    "philadelphia, pa": (39.9526, -75.1652),
    "pittsburgh, pa": (40.4406, -79.9959),
    "ponce, pr": (18.0111, -66.6141),
    "san juan, pr": (18.4655, -66.1057),
    "providence, ri": (41.8240, -71.4128),
    "charleston, sc": (32.7765, -79.9311),
    "columbia, sc": (34.0007, -81.0348),
    "greenville, sc": (34.8526, -82.3940),
    "pierre, sd": (44.3683, -100.3510),
    "rapid city, sd": (44.0805, -103.2310),
    "sioux falls, sd": (43.5460, -96.7313),
    "chattanooga, tn": (35.0456, -85.3097),
    "knoxville, tn": (35.9606, -83.9207),
    "memphis, tn": (35.1495, -90.0490),
    "nashville, tn": (36.1627, -86.7816),
    "amarillo, tx": (35.2220, -101.8313),
    "austin, tx": (30.2672, -97.7431),
    "corpus christi, tx": (27.8006, -97.3964),
    "dallas, tx": (32.7767, -96.7970),
    "el paso, tx": (31.7619, -106.4850),
    "houston, tx": (29.7604, -95.3698),
    "lubbock, tx": (33.5779, -101.8552),
    "san antonio, tx": (29.4241, -98.4936),
    "moab, ut": (38.5733, -109.5498),
    "salt lake city, ut": (40.7608, -111.8910),
    "st. george, ut": (37.0965, -113.5684),
    "arlington, va": (38.8816, -77.0910),
    "richmond, va": (37.5407, -77.4360),
    "roanoke, va": (37.2710, -79.9414),
    "virginia beach, va": (36.8529, -75.9780),
    "burlington, vt": (44.4759, -73.2121),
    "montpelier, vt": (44.2601, -72.5754),
    "olympia, wa": (47.0379, -122.9007),
    "seattle, wa": (47.6062, -122.3321),
    "spokane, wa": (47.6588, -117.4260),
    "yakima, wa": (46.6021, -120.5059),
    "green bay, wi": (44.5133, -88.0158),
    "madison, wi": (43.0731, -89.4012),
    "milwaukee, wi": (43.0389, -87.9065),
    "charleston, wv": (38.3498, -81.6326),
    "morgantown, wv": (39.6295, -79.9559),
    "casper, wy": (42.8666, -106.3131),
    "cheyenne, wy": (41.1400, -104.8202),
    "jackson, wy": (43.4799, -110.7624),
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


def cities_by_state() -> dict[str, list[str]]:
    """Built-in cities grouped by state postal code, both title-cased."""
    grouped: dict[str, list[str]] = {}
    for city in supported_cities():
        state = city.rsplit(",", 1)[1].strip().upper()
        grouped.setdefault(state, []).append(city)
    return grouped


def all_cities() -> list[str]:
    """Every built-in city - the full nationwide forecast sweep.

    The daily job passes this so the forecast layer covers all 50 states rather
    than whichever handful was hard-coded. Alerts do not need it: under national
    scope they arrive for the whole country in one request regardless.
    """
    return supported_cities()


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

    def get_absolute(self, url: str) -> Any:
        """Fetch a fully-qualified api.weather.gov URL.

        The API hands back absolute links (an alert's `affectedZones`), and
        rebuilding them into paths just to re-prepend base_url would be busywork.
        The host is checked so a URL from a payload cannot redirect the session -
        and its User-Agent - at somewhere unrelated.
        """
        if not isinstance(url, str) or not url.startswith(self.base_url + "/"):
            raise ValueError(f"Refusing to fetch off-API URL: {url!r}")
        resp = self._session.get(url, timeout=self.timeout)
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
        self,
        state: str | None = None,
        point: tuple[float, float] | None = None,
        national: bool = False,
    ) -> list[dict]:
        """Active alerts nationwide, for a state ("IL"), or for a coordinate.

        Note there is no `limit` parameter: api.weather.gov rejects one on
        /alerts/active with HTTP 400 ("Query parameter \"limit\" is not
        recognized"). Callers cap the count themselves.
        """
        if point is not None:
            params = {"point": f"{round(point[0], 4)},{round(point[1], 4)}"}
        elif state:
            params = {"area": state.upper()}
        elif national:
            # No area filter at all: the whole country in one response.
            params = None
        else:
            raise ValueError("get_active_alerts requires state, point, or national")

        return self.get("/alerts/active", params=params).get("features", []) or []

    def get_forecast_periods(self, grid_id: str, grid_x: int, grid_y: int) -> list[dict]:
        """The multi-day narrative forecast: one period per half-day."""
        data = self.get(f"/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast")
        return (data.get("properties") or {}).get("periods", []) or []

    # -- normalization -------------------------------------------------------

    @staticmethod
    def alert_location(feature: dict, fallback: str | None = None) -> str:
        """A place label for an alert that nobody requested by name.

        Under national scope there is no requesting city to borrow a label from,
        so the alert has to name itself. `areaDesc` is a semicolon-separated list
        of the counties or marine areas covered; its first entry is already in
        "Haskell, OK" shape, which matches how every other location in this app
        reads. Long lists are summarized rather than pasted, because the label is
        a UI string, not the alert's area of record - area_desc keeps that.
        """
        props = feature.get("properties") or {}
        area = _clean(props.get("areaDesc"))
        parts = [p.strip() for p in area.split(";") if p.strip()] if area else []
        if parts:
            extra = len(parts) - 1
            return f"{parts[0]} +{extra} more" if extra > 0 else parts[0]

        codes = (props.get("geocode") or {}).get("UGC") or []
        for code in codes:
            state = weather_zones.state_of(code)
            if state:
                return state
        return fallback or "United States"

    @staticmethod
    def alert_anchor(
        feature: dict,
        zone_resolver: Any = None,
        fallback: tuple[float, float] | None = None,
    ) -> tuple[float | None, float | None, str | None]:
        """Where to plot an alert, and how that was decided.

        Returns (latitude, longitude, geo_source). See weather_zones for why
        this cannot simply read the feature's geometry: four alerts in five do
        not have one.
        """
        point = weather_zones.coordinates_of(feature.get("geometry"))
        if point:
            return point[0], point[1], weather_zones.GEO_POLYGON

        props = feature.get("properties") or {}

        if zone_resolver is not None:
            zones = props.get("affectedZones") or []
            point = zone_resolver.resolve(zones)
            if point:
                return point[0], point[1], weather_zones.GEO_ZONE

        point = weather_zones.state_centroid((props.get("geocode") or {}).get("UGC") or [])
        if point:
            return point[0], point[1], weather_zones.GEO_STATE

        if fallback:
            return fallback[0], fallback[1], weather_zones.GEO_POINT

        # An offshore warning with an unresolved marine zone lands here. Leaving
        # the coordinates NULL keeps it searchable while keeping it off the
        # globe, which is better than inventing a landlocked position for it.
        return None, None, None

    @staticmethod
    def normalize_alert(
        feature: dict,
        location: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        zone_resolver: Any = None,
    ) -> dict | None:
        """Turn an /alerts/active GeoJSON feature into a document record.

        Returns None for an alert with no usable free text - a handful of
        administrative message types carry only structured fields, and embedding
        an empty string would put a meaningless vector in the index.

        `location`/`lat`/`lon` describe whoever asked for this alert and are
        optional: under national scope nobody did. They are used only as the last
        resort for placement, never in preference to the alert's own geography.
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

        fallback = (lat, lon) if lat is not None and lon is not None else None
        anchor_lat, anchor_lon, geo_source = WeatherClient.alert_anchor(
            feature, zone_resolver=zone_resolver, fallback=fallback
        )

        return {
            "id": f"alert:{alert_id}",
            "location": location or WeatherClient.alert_location(feature),
            "latitude": anchor_lat,
            "longitude": anchor_lon,
            "geo_source": geo_source,
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
            "geo_source": weather_zones.GEO_POINT,
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
        zone_resolver: Any = None,
        national_alert_limit: int | None = None,
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

        Under the default national scope, alerts do not come from the locations
        at all: they are fetched once for the whole country, and `locations`
        selects only which places get a narrative forecast. That decoupling is
        the point - the alert layer should not be blank in a state simply
        because no city there was listed.
        """
        sources = tuple(sources)
        scope = (alert_scope or DEFAULT_ALERT_SCOPE).lower()
        if scope not in VALID_ALERT_SCOPES:
            raise ValueError(
                f"Unknown alert scope {scope!r}; expected one of {', '.join(VALID_ALERT_SCOPES)}."
            )
        documents: dict[str, dict] = {}
        errors: list[str] = []
        want_alerts = SOURCE_ALERT in sources
        want_forecasts = SOURCE_FORECAST in sources

        # Alerts are per-state, so two locations in the same state would fetch
        # the same list twice. Cache per call rather than per process - active
        # alerts change by the minute and must not be stale across syncs.
        alerts_by_state: dict[str, list[dict]] = {}

        def keep_alert(feature: dict, location: str | None,
                       lat: float | None, lon: float | None) -> bool:
            doc = self.normalize_alert(feature, location, lat, lon, zone_resolver=zone_resolver)
            if not doc:
                return False
            # An alert covering two synced locations arrives twice under one id;
            # first write wins.
            documents.setdefault(doc["id"], doc)
            return True

        if want_alerts and scope == "national":
            cap = national_alert_limit if national_alert_limit is not None else NATIONAL_ALERT_LIMIT
            try:
                features = self.get_active_alerts(national=True)
                kept = 0
                for feature in features:
                    if kept >= cap:
                        break
                    if keep_alert(feature, None, None, None):
                        kept += 1
                log(f"  nationwide: {kept} alerts (of {len(features)} active)")
                if len(features) > cap:
                    log(f"  nationwide: capped at {cap}; {len(features) - cap} alert(s) skipped")
            except requests.RequestException as err:
                errors.append(f"nationwide alerts failed ({err})")
                log(f"  nationwide alerts failed ({err})")

        for raw_location in locations:
            lat, lon = resolve_location(raw_location)

            try:
                point = self.get_point(lat, lon)
            except requests.RequestException as err:
                errors.append(f"{raw_location}: grid lookup failed ({err})")
                log(f"  {raw_location}: grid lookup failed, skipping ({err})")
                continue

            location = point["location"]

            if want_alerts and scope != "national":
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
                        if keep_alert(feature, location, lat, lon):
                            kept += 1
                    log(f"  {location}: {kept} alerts")
                except requests.RequestException as err:
                    errors.append(f"{location}: alerts failed ({err})")
                    log(f"  {location}: alerts failed ({err})")

            if want_forecasts:
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
