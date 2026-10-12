"""Location edits preserve AP ownership; the map only exposes confirmed records."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from backend.store import Store
from backend.test_app import FakeUpdater
from backend.venue_locations import (
    SEARCH_CACHE_LIMIT,
    SEARCH_USER_AGENT,
    initialize_locations,
    save_location,
)
from tests.fixtures.catalog import write_catalog, write_imports
from tests.support import create_app


def sample_chart(number, *, venue="【测试】机厅", is_ap=True):
    return {
        "chart_key": f"{number}:3",
        "id": number,
        "title": f"Song {number}",
        "venue": venue,
        "is_ap": is_ap,
        "difficulty": 3,
        "difficulty_name": "MASTER",
        "level": "13+",
        "constant": 13.7,
        "jacket_url": f"/jackets/{number}.png",
        "achievement": None,
        "provider_achievement": 100.9,
        "status": "ap" if is_ap else "incomplete",
        "type": "standard",
    }


def search_payload():
    return {
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [116.4, 39.9]},
                "properties": {
                    "name": "测试广场",
                    "city": "测试城",
                    "street": "测试路",
                    "housenumber": "1号",
                },
            }
        ]
    }


class VenueLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = write_catalog(self.root / "catalog")
        self.store = Store(
            runtime=self.root / "runtime",
            import_dir=write_imports(self.root / "imports"),
            catalog_dir=self.catalog,
        )
        self.app = create_app(self.store, updater=FakeUpdater())
        self.client = TestClient(self.app)
        self.geocoder = patch.dict(
            os.environ, {"MAIMAI_GEOCODER_URL": "https://geocoder.invalid/api/"}
        )
        self.geocoder.start()

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.geocoder.stop()
        self.temp.cleanup()

    def test_location_edit_is_persistent_and_does_not_rename_or_reassign_ap(self):
        chart = self.store.chart("2000:standard:3")
        save_location(self.store, {"name": "【测试】原机厅", "latitude": 39.9, "longitude": 116.4})
        self.store.save_ap_record(
            chart["chart_key"],
            {
                "venue": "【测试】原机厅",
                "expected_revision": chart["record_revision"],
                "request_id": str(uuid4()),
            },
        )
        before = self.store.chart(chart["chart_key"])
        with self.store.db() as conn:
            original = dict(
                conn.execute("SELECT * FROM venues WHERE name='【测试】原机厅'").fetchone()
            )
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        payload = {
            "name": "【测试】原机厅",
            "latitude": 39.9,
            "longitude": 116.4,
        }
        saved = self.client.put("/api/venues/location", json=payload)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["location"]["coordinate_system"], "WGS84")
        self.assertEqual(
            set(saved.json()["location"]),
            {"latitude", "longitude", "coordinate_system", "updated_at"},
        )
        payload.update(latitude=39.8)
        self.assertEqual(self.client.put("/api/venues/location", json=payload).status_code, 200)
        self.assertEqual(self.store.chart(chart["chart_key"]), before)
        with self.store.db() as conn:
            self.assertEqual(
                dict(conn.execute("SELECT * FROM venues WHERE name='【测试】原机厅'").fetchone()),
                original,
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
        reopened = Store(runtime=self.root / "runtime", catalog_dir=self.catalog)
        initialize_locations(reopened)
        try:
            with reopened.db() as conn:
                value = conn.execute("SELECT * FROM venue_locations").fetchone()
            self.assertEqual(value["latitude"], 39.8)
            self.assertEqual(set(value.keys()), {"name", "latitude", "longitude", "updated_at"})
        finally:
            reopened.close()

    def test_new_venue_saves_only_display_name_and_location(self):
        saved = save_location(
            self.store,
            {"name": "【测试】新机厅", "latitude": 39.9, "longitude": 116.4},
        )
        self.assertEqual(saved["location"]["latitude"], 39.9)
        self.assertEqual(
            set(saved["location"]), {"latitude", "longitude", "coordinate_system", "updated_at"}
        )
        with patch.object(self.store, "all_charts", return_value=[]):
            response = self.client.get("/api/venues/map")
        row = next(item for item in response.json()["items"] if item["name"] == "【测试】新机厅")
        self.assertEqual(row["total"], 0)
        self.assertEqual(row["jackets"], [])
        self.assertEqual(row["city"], "测试")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_old_schema_migration_preserves_names_coordinates_and_ap_data(self):
        with self.store.db() as conn:
            conn.execute("DROP TABLE venue_locations")
            conn.execute(
                "CREATE TABLE venue_locations(name TEXT PRIMARY KEY, place_name TEXT, address TEXT, "
                "latitude REAL, longitude REAL, coordinate_system TEXT, accuracy TEXT, "
                "closed_status TEXT, source TEXT, updated_at TEXT)"
            )
            conn.executemany(
                "INSERT INTO venue_locations VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "【测试】历史机厅",
                        "旧门店",
                        "旧地址",
                        39.9,
                        116.4,
                        "WGS84",
                        "exact",
                        "closed",
                        "old",
                        "then",
                    ),
                    (
                        "【测试】未定位",
                        "未知门店",
                        "旧地址",
                        None,
                        None,
                        "WGS84",
                        "unknown",
                        "unknown",
                        "old",
                        "before",
                    ),
                ],
            )
            conn.execute(
                "INSERT INTO legacy VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "2000:standard:3",
                    "high",
                    13.3,
                    1,
                    "【测试】历史机厅",
                    "Example",
                    "fixture",
                    1,
                    1,
                ),
            )
            before = {
                table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                for table in (
                    "legacy",
                    "manual",
                    "ap_pending",
                    "ap_confirmations",
                    "venues",
                    "events",
                )
            }
        initialize_locations(self.store)
        initialize_locations(self.store)
        with self.store.db() as conn:
            self.assertEqual(
                {row[1] for row in conn.execute("PRAGMA table_info(venue_locations)")},
                {"name", "latitude", "longitude", "updated_at"},
            )
            self.assertEqual(
                [tuple(row) for row in conn.execute("SELECT * FROM venue_locations ORDER BY name")],
                [
                    ("【测试】历史机厅", 39.9, 116.4, "then"),
                    ("【测试】未定位", None, None, "before"),
                ],
            )
            for table, rows in before.items():
                self.assertEqual(
                    [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")], rows
                )

    def test_invalid_coordinates_and_names_do_not_write_partial_venues(self):
        cases = [
            {"latitude": 90.1, "longitude": 0},
            {"latitude": 0, "longitude": -180.1},
            {"latitude": None},
            {"longitude": None},
            {"latitude": True, "longitude": 0},
            {"latitude": "39.9", "longitude": 116.4},
            {"latitude": float("nan"), "longitude": 0},
            {"latitude": 0, "longitude": float("inf")},
            {"name": " "},
            {"name": "x" * 201},
            {"name": "bad\nname"},
            {"coordinate_system": "GCJ-02"},
            {"source": "forged"},
            {"closed_status": "closed"},
            {"address": "unused address"},
            {"place_name": "unused place"},
            {"accuracy": "exact"},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                response = self.client.put(
                    "/api/venues/location",
                    content=json.dumps(
                        {"name": "【测试】无效", "latitude": 39.9, "longitude": 116.4, **extra}
                    ),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("NaN", response.text)
        with self.store.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM venue_locations").fetchone()[0], 0)
            self.assertIsNone(
                conn.execute("SELECT name FROM venues WHERE name='【测试】无效'").fetchone()
            )

    def test_map_limits_jackets_and_counts_pending_separately_from_confirmed(self):
        chart = self.store.chart("2000:standard:3")
        save_location(self.store, {"name": "【测试】待核准", "latitude": 39.9, "longitude": 116.4})
        self.store.save_ap_record(
            chart["chart_key"],
            {
                "venue": "【测试】待核准",
                "expected_revision": chart["record_revision"],
                "request_id": str(uuid4()),
            },
        )
        charts = [sample_chart(number) for number in range(1, 8)]
        charts += [sample_chart(8, is_ap=False), sample_chart(9, venue="【测试】其他")]
        with patch.object(self.store, "all_charts", return_value=charts):
            response = self.client.get("/api/venues/map")
        rows = {item["name"]: item for item in response.json()["items"]}
        self.assertEqual(rows["【测试】机厅"]["total"], 7)
        self.assertEqual(len(rows["【测试】机厅"]["jackets"]), 3)
        self.assertEqual(rows["【测试】机厅"]["jackets"][0]["achievement"], 100.9)
        self.assertIn("thumbnail_url", rows["【测试】机厅"]["jackets"][0])
        self.assertEqual(rows["【测试】待核准"]["total"], 0)
        self.assertEqual(rows["【测试】待核准"]["pending_count"], 1)
        self.assertEqual(rows["【测试】待核准"]["jackets"], [])
        self.assertIsNone(rows["【测试】机厅"]["location"])

    def test_chart_pagination_is_exact_venue_confirmed_only_and_stable(self):
        charts = [sample_chart(number) for number in range(7, 0, -1)]
        charts += [sample_chart(8, is_ap=False), sample_chart(9, venue="【测试】机厅分店")]
        with patch.object(self.store, "all_charts", return_value=charts):
            response = self.client.get(
                "/api/venues/charts", params={"name": "【测试】机厅", "offset": 2, "limit": 3}
            )
            empty = self.client.get(
                "/api/venues/charts", params={"name": "【测试】机厅", "offset": 100}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["total"], 7)
        self.assertEqual([item["id"] for item in response.json()["items"]], [3, 4, 5])
        self.assertEqual(empty.json()["items"], [])
        for params in ({"limit": 101}, {"offset": -1}, {"limit": 0}):
            self.assertEqual(
                self.client.get(
                    "/api/venues/charts", params={"name": "机厅", **params}
                ).status_code,
                422,
            )

    def test_search_cache_survives_service_recreation_and_rate_limit_is_shared(self):
        reply = httpx.Response(
            200,
            json=search_payload(),
            request=httpx.Request("GET", "https://geocoder.invalid/api/"),
        )
        with (
            patch("backend.venue_locations.time.time", return_value=1000),
            patch("backend.venue_locations.httpx.get", return_value=reply) as fetch,
        ):
            result = self.client.get("/api/venues/search", params={"q": "广场", "city": "测试城"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["items"][0]["latitude"], 39.9)
            self.assertEqual(result.json()["items"][0]["longitude"], 116.4)
            self.assertEqual(result.json()["provider"], "Photon")
            self.assertEqual(fetch.call_args.kwargs["params"]["q"], "测试城 广场")
            self.assertEqual(fetch.call_args.kwargs["headers"]["User-Agent"], SEARCH_USER_AGENT)
            fresh_client = TestClient(create_app(self.store, updater=FakeUpdater()))
            try:
                cached = fresh_client.get(
                    "/api/venues/search", params={"q": "广场", "city": "测试城"}
                )
                busy = fresh_client.get("/api/venues/search", params={"q": "另一个地点"})
            finally:
                fresh_client.close()
            self.assertEqual(cached.json(), result.json())
            self.assertEqual(busy.status_code, 429)
            self.assertEqual(fetch.call_count, 1)

    def test_search_errors_disable_and_malformed_responses_are_safe(self):
        with (
            patch.dict(os.environ, {"MAIMAI_GEOCODER_URL": ""}),
            patch("backend.venue_locations.httpx.get") as fetch,
        ):
            result = self.client.get("/api/venues/search", params={"q": "地点"})
            self.assertEqual(result.status_code, 503)
            self.assertEqual(result.json()["detail"]["code"], "search_disabled")
            fetch.assert_not_called()
        with patch(
            "backend.venue_locations.httpx.get", side_effect=httpx.ConnectTimeout("internal")
        ):
            result = self.client.get("/api/venues/search", params={"q": "地点"})
            self.assertEqual(result.status_code, 502)
            self.assertNotIn("internal", result.text)
        with self.store.db() as conn:
            conn.execute("DELETE FROM venue_geocoder_rate_limit")
        reply = httpx.Response(
            200, json={"bad": "data"}, request=httpx.Request("GET", "https://geocoder.invalid/api/")
        )
        with patch("backend.venue_locations.httpx.get", return_value=reply):
            result = self.client.get("/api/venues/search", params={"q": "地点"})
            self.assertEqual(result.status_code, 502)
        with self.store.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM venue_geocoder_cache").fetchone()[0], 0
            )

    def test_search_results_and_cache_are_bounded(self):
        payload = search_payload()
        payload["features"] *= 20
        payload["features"][0] = {
            "geometry": {"type": "Point", "coordinates": [999, 39]},
            "properties": {},
        }
        with self.store.db() as conn:
            conn.executemany(
                "INSERT INTO venue_geocoder_cache VALUES(?,?,?)",
                [(str(number), "{}", 1000 + number) for number in range(SEARCH_CACHE_LIMIT + 10)],
            )
        reply = httpx.Response(
            200, json=payload, request=httpx.Request("GET", "https://geocoder.invalid/api/")
        )
        with (
            patch("backend.venue_locations.time.time", return_value=2000),
            patch("backend.venue_locations.httpx.get", return_value=reply),
        ):
            result = self.client.get("/api/venues/search", params={"q": "广场"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()["items"]), 5)
        with self.store.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM venue_geocoder_cache").fetchone()[0],
                SEARCH_CACHE_LIMIT,
            )


if __name__ == "__main__":
    unittest.main()
