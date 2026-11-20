import csv
import io
import os
import zipfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support import FakeUpdater, FixtureAppCase, create_app


class ExportTests(FixtureAppCase):
    def test_venue_csv_lists_imported_and_app_records(self):
        with self.store.db() as conn:
            conn.execute(
                "INSERT INTO ap_confirmations VALUES(?,?,?,?,?,?,?)",
                ("131:3", "【测试】机厅", "2026-11-01", "2026-11-02", "s", "c", "{}"),
            )
        response = self.client.get("/api/export/venues.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(
            [(r["chart_key"], r["venue"], r["source"]) for r in rows],
            [("22:4", "Fixture Arcade", "import"), ("131:3", "【测试】机厅", "app")],
        )

    def test_asset_pack_is_a_zip_of_every_asset(self):
        assets = self.root / "assets"
        (assets / "fonts").mkdir(parents=True)
        (assets / "fonts/a.ttf").write_bytes(b"font")
        (assets / "logo.png").write_bytes(b"png")
        with patch.dict(os.environ, {"MAIMAI_ASSETS_DIR": str(assets)}):
            client = TestClient(create_app(self.store, updater=FakeUpdater()))
            self.addCleanup(client.close)
            response = client.get("/api/export/assets.zip")
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["fonts/a.ttf", "logo.png"])
