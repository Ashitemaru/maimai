"""Application preferences persist independently of scores and catalog data."""

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.store import Store
from backend.test_app import FakeUpdater
from tests.support import ROOT, create_app


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog_dir = Path(self.tmp.name) / "catalog"
        self.catalog_dir.mkdir()
        shutil.copy2(ROOT / "data/catalog/catalog.sqlite", self.catalog_dir / "catalog.sqlite")
        self.store = self.new_store()
        self.client = TestClient(create_app(self.store, updater=FakeUpdater()))

    def new_store(self):
        return Store(runtime=self.tmp.name, catalog_dir=self.catalog_dir)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def score_state(self):
        with self.store.db() as conn:
            result = {
                table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("legacy", "manual", "provider", "events")
            }
            result["metadata"] = [
                tuple(row)
                for row in conn.execute(
                    "SELECT * FROM metadata WHERE key != 'preferences' ORDER BY key"
                )
            ]
        return result

    def test_initial_defaults_are_persisted_before_first_get_and_get_is_not_cached(self):
        expected = {"default_constant_min": 13.0, "default_constant_max": 13.9}
        self.assertEqual(self.store.meta("preferences"), expected)
        response = self.client.get("/api/settings/preferences")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.new_store().preferences(), expected)

    def test_existing_personal_database_gets_defaults_without_changing_scores(self):
        with self.store.db() as conn:
            conn.execute("DELETE FROM metadata WHERE key='preferences'")
        before = self.score_state()
        rebuilt = self.new_store()
        self.assertEqual(
            rebuilt.preferences(), {"default_constant_min": 13.0, "default_constant_max": 13.9}
        )
        self.assertEqual(self.score_state(), before)

    def test_update_survives_store_and_app_recreation_and_catalog_reload(self):
        expected = {"default_constant_min": 12.6, "default_constant_max": 14.9}
        response = self.client.put("/api/settings/preferences", json=expected)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.assertEqual(self.client.get("/api/settings/preferences").json(), expected)
        rebuilt = self.new_store()
        self.assertEqual(rebuilt.preferences(), expected)
        rebuilt.reload_catalog()
        self.assertEqual(rebuilt.preferences(), expected)
        with TestClient(create_app(rebuilt, updater=FakeUpdater())) as recreated:
            self.assertEqual(recreated.get("/api/settings/preferences").json(), expected)

    def test_invalid_payloads_do_not_overwrite_the_previous_range(self):
        saved = {"default_constant_min": 12.5, "default_constant_max": 14.0}
        self.assertEqual(self.client.put("/api/settings/preferences", json=saved).status_code, 200)
        invalid = [
            {},
            {"default_constant_min": 13},
            saved | {"default_constant_min": -0.1},
            saved | {"default_constant_max": 20.1},
            saved | {"default_constant_min": 14.1},
            saved | {"default_constant_min": 13.01},
            saved | {"default_constant_max": 14.99},
            saved | {"default_constant_min": "13.0"},
            saved | {"default_constant_min": True},
            saved | {"default_constant_max": None},
            saved | {"unexpected": 1},
            saved | {"default_constant_min": float("nan")},
            saved | {"default_constant_max": float("inf")},
            saved | {"default_constant_min": float("-inf")},
        ]
        for value in invalid:
            with self.subTest(value=value):
                # Raw content also exercises rejection of JSON decoder NaN/Infinity values.
                response = self.client.put(
                    "/api/settings/preferences",
                    content=json.dumps(value),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.client.get("/api/settings/preferences").json(), saved)
        response = self.client.put(
            "/api/settings/preferences",
            content="{broken",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.new_store().preferences(), saved)

    def test_full_supported_range_is_independent_of_catalog_constants(self):
        for lower, upper in ((0, 20), (0.1, 0.9), (13.3, 13.3), (20, 20)):
            with self.subTest(lower=lower, upper=upper):
                value = {"default_constant_min": lower, "default_constant_max": upper}
                response = self.client.put("/api/settings/preferences", json=value)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), value)
                self.assertEqual(self.new_store().preferences(), value)

    def test_preferences_preserve_scores_metadata_and_catalog_and_enforce_local_origin(self):
        before = self.score_state()
        catalog_digest = hashlib.sha256(
            (self.catalog_dir / "catalog.sqlite").read_bytes()
        ).hexdigest()
        value = {"default_constant_min": 10.0, "default_constant_max": 15.5}
        blocked = self.client.put(
            "/api/settings/preferences", json=value, headers={"Origin": "https://external.example"}
        )
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(
            self.store.preferences(), {"default_constant_min": 13.0, "default_constant_max": 13.9}
        )
        allowed = self.client.put(
            "/api/settings/preferences", json=value, headers={"Origin": "http://testserver"}
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(self.score_state(), before)
        self.assertEqual(
            hashlib.sha256((self.catalog_dir / "catalog.sqlite").read_bytes()).hexdigest(),
            catalog_digest,
        )

    def test_failed_atomic_write_keeps_the_entire_previous_range(self):
        before = self.store.preferences()
        with self.store.db() as conn:
            conn.executescript("""
                CREATE TRIGGER reject_preferences BEFORE UPDATE ON metadata
                WHEN NEW.key='preferences'
                BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_preferences(
                {"default_constant_min": 11.0, "default_constant_max": 12.0}
            )
        self.assertEqual(self.store.preferences(), before)


if __name__ == "__main__":
    unittest.main()
