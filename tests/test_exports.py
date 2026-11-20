import csv
import io
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.store import Store
from backend.venue_locations import save_location
from tests.support import ROOT, create_app
from tests.test_app import FakeUpdater


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "catalog").mkdir()
        shutil.copy2(ROOT / "data/catalog/catalog.sqlite", root / "catalog/catalog.sqlite")
        self.store = Store(runtime=root / "runtime", catalog_dir=root / "catalog")
        self.assets = root / "assets"
        (self.assets / "fonts").mkdir(parents=True)
        (self.assets / "fonts/a.ttf").write_bytes(b"font")
        (self.assets / "logo.png").write_bytes(b"png")
        environment = patch.dict(os.environ, {"MAIMAI_ASSETS_DIR": str(self.assets)})
        environment.start()
        self.addCleanup(environment.stop)
        self.client = TestClient(create_app(self.store, updater=FakeUpdater()))
        self.addCleanup(self.client.close)

    def rows(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertTrue(response.content.startswith("﻿".encode()))
        return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_venue_records_include_import_and_app_sources(self):
        with self.store.db() as conn:
            conn.execute(
                "INSERT INTO ap_confirmations VALUES(?,?,?,?,?,?,?)",
                ("131:3", "【测试】机厅", "2026-11-01", "2026-11-02", "s", "c", "{}"),
            )
        rows = self.rows("/api/export/venues.csv")
        self.assertEqual(
            [(r["chart_key"], r["venue"], r["source"], r["recorded_at"]) for r in rows],
            [
                ("22:4", "Fixture Arcade", "import", ""),
                ("131:3", "【测试】机厅", "app", "2026-11-01"),
            ],
        )
        self.assertEqual((rows[1]["title"], rows[1]["difficulty"]), ("Link", "MASTER"))

    def test_venue_locations_list_every_venue(self):
        save_location(self.store, {"name": "【测试】机厅", "latitude": 39.9, "longitude": 116.4})
        rows = {row["venue"]: row for row in self.rows("/api/export/venue-locations.csv")}
        self.assertEqual(rows["【测试】机厅"]["city"], "测试")
        self.assertEqual(float(rows["【测试】机厅"]["latitude"]), 39.9)
        self.assertEqual(rows["Fixture Arcade"]["latitude"], "")

    def test_asset_pack_zip_streams_every_file(self):
        response = self.client.get("/api/export/assets.zip")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("mai-journal-assets.zip", response.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["fonts/a.ttf", "logo.png"])
            self.assertEqual(archive.read("logo.png"), b"png")
        self.assertEqual(list(self.store.runtime.glob(".assets-*")), [])

    def test_version_token_changes_after_personal_write(self):
        token = self.client.get("/api/version").json()["token"]
        self.assertEqual(self.client.get("/api/charts").status_code, 200)
        self.assertEqual(self.client.get("/api/version").json()["token"], token)
        self.client.put(
            "/api/settings/preferences",
            json={"default_constant_min": 12.0, "default_constant_max": 13.0},
        )
        self.assertNotEqual(self.client.get("/api/version").json()["token"], token)


if __name__ == "__main__":
    unittest.main()
