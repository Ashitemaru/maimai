"""Private venue locations and user-triggered, cached place lookup."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from .store import now
from .venue_summary import summarize_venues

SEARCH_CACHE_SECONDS = 7 * 24 * 60 * 60
SEARCH_CACHE_LIMIT = 512
SEARCH_RESULT_LIMIT = 6
DEFAULT_GEOCODER_URL = "https://photon.komoot.io/api/"
SEARCH_USER_AGENT = "mai-journal/1.0 (+https://github.com/Ashitemaru/maimai)"
MAP_CHART_FIELDS = (
    "chart_key",
    "title",
    "jacket_url",
    "difficulty",
    "difficulty_name",
    "level",
    "constant",
    "status",
    "type",
)


class VenueLocationInput(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    name: str = Field(min_length=1, max_length=200)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def numeric_coordinate(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("坐标须为数值")
        return value

    @field_validator("name")
    @classmethod
    def plain_text(cls, value):
        if any(ord(character) < 32 for character in value):
            raise ValueError("地点文字不可包含控制字符")
        return value


LOCATION_TABLE_SQL = """
    CREATE TABLE venue_locations(
        name TEXT PRIMARY KEY REFERENCES venues(name),
        latitude REAL, longitude REAL, updated_at TEXT NOT NULL,
        CHECK((latitude IS NULL AND longitude IS NULL) OR
              (latitude IS NOT NULL AND longitude IS NOT NULL AND
               latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180))
    )
"""


def initialize_locations(store):
    with store.db() as conn:
        # Table replacement and row copying form one transaction. Startup either
        # keeps the old schema intact or preserves every name and map coordinate.
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(venue_locations)")}
        if not columns:
            conn.execute(LOCATION_TABLE_SQL)
        elif columns != {"name", "latitude", "longitude", "updated_at"}:
            conn.execute("ALTER TABLE venue_locations RENAME TO venue_locations_previous")
            conn.execute(LOCATION_TABLE_SQL)
            conn.execute(
                "INSERT INTO venue_locations(name,latitude,longitude,updated_at) "
                "SELECT name,latitude,longitude,updated_at FROM venue_locations_previous"
            )
            conn.execute("DROP TABLE venue_locations_previous")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS venue_geocoder_cache("
            "cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS venue_geocoder_rate_limit("
            "id INTEGER PRIMARY KEY CHECK(id=1), last_attempt REAL NOT NULL)"
        )


def location_from_row(row):
    if row is None:
        return None
    return {
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "coordinate_system": "WGS84",
        "updated_at": row["updated_at"],
    }


def save_location(store, value):
    """Save a display name and WGS84 point without changing AP ownership."""
    if not isinstance(value, VenueLocationInput):
        value = VenueLocationInput.model_validate(value)
    stamp = now()
    with store.db() as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO venues(name,created_at,last_used_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO NOTHING",
            (value.name, stamp, ""),
        )
        conn.execute(
            "INSERT INTO venue_locations(name,latitude,longitude,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET latitude=excluded.latitude,"
            "longitude=excluded.longitude,updated_at=excluded.updated_at",
            (value.name, value.latitude, value.longitude, stamp),
        )
        row = conn.execute("SELECT * FROM venue_locations WHERE name=?", (value.name,)).fetchone()
    return {"name": value.name, "location": location_from_row(row)}


def confirmed_venue_charts(charts, name):
    items = [chart for chart in charts if chart.get("venue") == name and chart.get("is_ap")]
    return sorted(
        items,
        key=lambda chart: (
            -(chart.get("constant") or 0),
            int(chart.get("id") or chart.get("song_id") or 0),
            chart.get("difficulty") or 0,
            chart["chart_key"],
        ),
    )


def venue_map(store):
    with store.db() as conn:
        conn.execute("BEGIN")
        charts = store.all_charts(connection=conn)
        names = [row["name"] for row in conn.execute("SELECT name FROM venues")]
        pending = [dict(row) for row in conn.execute("SELECT venue FROM ap_pending")]
        locations = {
            row["name"]: location_from_row(row)
            for row in conn.execute("SELECT * FROM venue_locations")
        }
    result = summarize_venues(charts, names, pending)
    items = []
    for row in result["items"]:
        jackets = []
        for chart in confirmed_venue_charts(charts, row["name"])[:3]:
            jacket = {key: chart.get(key) for key in MAP_CHART_FIELDS}
            jacket["achievement"] = (
                chart["achievement"]
                if chart.get("achievement") is not None
                else chart.get("provider_achievement")
            )
            jackets.append(jacket)
        items.append(
            {
                "name": row["name"],
                "city": row["city"],
                "total": row["total"],
                "pending_count": row["pending_count"],
                "location": locations.get(row["name"]),
                "jackets": jackets,
            }
        )
    return {"items": items}


class PlaceSearchError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def normalize_search_result(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise ValueError("invalid geocoder response")
    items = []
    for row in payload["features"][:SEARCH_RESULT_LIMIT]:
        if not isinstance(row, dict):
            continue
        geometry = row.get("geometry")
        properties = row.get("properties")
        if not isinstance(geometry, dict) or not isinstance(properties, dict):
            continue
        if geometry.get("type") != "Point":
            continue
        try:
            longitude, latitude = map(float, geometry["coordinates"])
        except (KeyError, ValueError, TypeError):
            continue
        if not (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            continue
        parts = [
            str(properties.get(key) or "")
            for key in ("country", "state", "city", "district", "street", "housenumber")
        ]
        address = " ".join(dict.fromkeys(part for part in parts if part))[:1000]
        place_name = str(properties.get("name") or address)[:300]
        items.append(
            {
                "place_name": place_name,
                "address": address,
                "latitude": latitude,
                "longitude": longitude,
                "coordinate_system": "WGS84",
            }
        )
    return {
        "items": items,
        "provider": "Photon",
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": "https://www.openstreetmap.org/copyright",
    }


def search_places(store, query, city=""):
    """Only called by explicit user search; never an autocomplete or batch job.

    MAIMAI_GEOCODER_URL selects a Photon-compatible search endpoint; setting it
    to an empty string disables remote lookup while retaining manual map editing.
    Shared SQLite reservations enforce the one-request-per-second ceiling even if
    multiple application workers are used; cache hits do not contact the provider.
    """
    endpoint = os.environ.get("MAIMAI_GEOCODER_URL", DEFAULT_GEOCODER_URL).strip()
    if not endpoint:
        raise PlaceSearchError(503, "search_disabled", "地点搜索暂不可用，请稍后重试。")
    try:
        parsed = urlsplit(endpoint)
        valid_url = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.fragment
    except ValueError:
        valid_url = False
    if not valid_url:
        raise PlaceSearchError(503, "search_unavailable", "地点搜索暂不可用，请稍后重试。")
    query = " ".join(query.split())
    city = " ".join(city.split())
    if not query:
        raise PlaceSearchError(422, "empty_query", "请输入地点名称。")
    search_query = f"{city} {query}" if city and city not in query else query
    cache_key = hashlib.sha256(f"{endpoint}\n{search_query}".encode()).hexdigest()
    stamp = time.time()
    with store.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cached = conn.execute(
            "SELECT result_json FROM venue_geocoder_cache WHERE cache_key=? AND created_at>?",
            (cache_key, stamp - SEARCH_CACHE_SECONDS),
        ).fetchone()
        if cached:
            return json.loads(cached["result_json"])
        previous = conn.execute(
            "SELECT last_attempt FROM venue_geocoder_rate_limit WHERE id=1"
        ).fetchone()
        if previous and stamp - previous["last_attempt"] < 1.0:
            raise PlaceSearchError(429, "search_busy", "搜索过于频繁，请稍后再试。")
        conn.execute(
            "INSERT INTO venue_geocoder_rate_limit(id,last_attempt) VALUES(1,?) "
            "ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt",
            (stamp,),
        )
    try:
        response = httpx.get(
            endpoint,
            params={"q": search_query, "limit": SEARCH_RESULT_LIMIT},
            headers={"User-Agent": SEARCH_USER_AGENT, "Accept-Language": "zh-CN,zh,en"},
            timeout=httpx.Timeout(6.0, connect=3.0),
            follow_redirects=False,
        )
        response.raise_for_status()
        if len(response.content) > 512 * 1024:
            raise ValueError("oversized geocoder response")
        result = normalize_search_result(response.json())
    except (httpx.HTTPError, ValueError, TypeError):
        raise PlaceSearchError(
            502, "search_unavailable", "地点搜索暂不可用，请稍后重试。"
        ) from None
    with store.db() as conn:
        conn.execute(
            "INSERT INTO venue_geocoder_cache(cache_key,result_json,created_at) VALUES(?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "result_json=excluded.result_json,created_at=excluded.created_at",
            (cache_key, json.dumps(result, ensure_ascii=False), stamp),
        )
        conn.execute(
            "DELETE FROM venue_geocoder_cache WHERE created_at<? OR cache_key NOT IN "
            "(SELECT cache_key FROM venue_geocoder_cache ORDER BY created_at DESC LIMIT ?)",
            (stamp - SEARCH_CACHE_SECONDS, SEARCH_CACHE_LIMIT),
        )
    return result
