"""Artwork caching must never cache private score responses or resize export sources."""

import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from backend.auth import hash_master_key
from backend.jackets import IMMUTABLE_CACHE, REVALIDATE_CACHE, JacketPreviews
from backend.store import Store
from backend.test_app import FakeUpdater
from tests.fixtures.catalog import write_catalog
from tests.support import create_app

MASTER_KEY = "isolated-jacket-test-master-key"
MASTER_KEY_HASH = hash_master_key(MASTER_KEY)


class JacketPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = write_catalog(self.root / "catalog")
        (self.catalog / "jackets").mkdir()
        self.original = self.catalog / "jackets/22.png"
        Image.new("RGB", (400, 400), "#f280a2").save(self.original)
        self.store = Store(runtime=self.root / "runtime", catalog_dir=self.catalog)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        (self.assets / "badge.png").write_bytes(b"public artwork")
        (self.assets / "manifest.json").write_text("{}")
        frontend = self.root / "frontend"
        (frontend / "assets").mkdir(parents=True)
        (frontend / "index.html").write_text("login shell")
        (frontend / "assets/index-AbC12345.js").write_text("hashed bundle")
        self.environment = patch.dict(
            os.environ,
            {
                "MAIMAI_ENV": "test",
                "MAIMAI_AUTH_DISABLED": "0",
                "MAIMAI_MASTER_KEY_HASH": MASTER_KEY_HASH,
                "MAIMAI_ASSETS_DIR": str(self.assets),
                "MAIMAI_FRONTEND_DIR": str(frontend),
            },
        )
        self.environment.start()
        self.app = create_app(self.store, updater=FakeUpdater())
        self.client = TestClient(self.app, base_url="https://testserver")
        self.previews = self.app.state.jacket_previews

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.environment.stop()
        self.temp.cleanup()

    def login(self):
        self.assertEqual(
            self.client.post("/api/auth/login", json={"master_key": MASTER_KEY}).status_code, 200
        )

    def test_preview_auth_dimensions_cache_and_original_export_source(self):
        url = self.previews.url("/jackets/22.png")
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get("/jackets/22.png").status_code, 401)
        self.login()
        with patch.object(self.previews, "_render", wraps=self.previews._render) as render:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/webp")
            self.assertEqual(response.headers["cache-control"], IMMUTABLE_CACHE)
            with Image.open(io.BytesIO(response.content)) as picture:
                self.assertEqual((picture.format, picture.size), ("WEBP", (160, 160)))
            self.assertEqual(self.client.get(url).content, response.content)
            for condition in (response.headers["etag"], "W/" + response.headers["etag"], "*"):
                unchanged = self.client.get(url, headers={"If-None-Match": condition})
                self.assertEqual((unchanged.status_code, unchanged.content), (304, b""))
                self.assertEqual(unchanged.headers["cache-control"], IMMUTABLE_CACHE)
            self.assertEqual(render.call_count, 1)
        original = self.client.get("/jackets/22.png")
        self.assertEqual(original.content, self.original.read_bytes())
        self.assertEqual(original.headers["cache-control"], REVALIDATE_CACHE)
        with Image.open(io.BytesIO(original.content)) as picture:
            self.assertEqual(picture.size, (400, 400))
        self.assertEqual(
            self.client.get(
                "/jackets/22.png", headers={"If-None-Match": original.headers["etag"]}
            ).status_code,
            304,
        )

    def test_source_replacement_invalidates_url_even_when_mtime_is_preserved(self):
        self.login()
        previous = self.previews.url("/jackets/22.png")
        response = self.client.get(previous)
        before = self.original.stat()
        replacement = self.original.with_suffix(".new.png")
        Image.new("RGB", (400, 400), "blue").save(replacement)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(self.original)
        current = self.previews.url("/jackets/22.png")
        self.assertNotEqual(previous, current)
        self.assertEqual(self.client.get(previous).status_code, 404)
        updated = self.client.get(current, headers={"If-None-Match": response.headers["etag"]})
        self.assertEqual(updated.status_code, 200)
        self.assertNotEqual(response.content, updated.content)

    def test_catalog_snapshot_artwork_takes_precedence_and_has_new_revision(self):
        current = [self.catalog]
        previews = JacketPreviews(self.root / "snapshot-cache", lambda: current)
        old = previews.url("/jackets/22.png")
        snapshot = self.root / "snapshot"
        (snapshot / "jackets").mkdir(parents=True)
        Image.new("RGB", (400, 400), "green").save(snapshot / "jackets/22.png")
        current[:] = [snapshot, self.catalog]
        self.assertNotEqual(previews.url("/jackets/22.png"), old)

    def test_concurrent_requests_render_once_and_restart_reuses_disk_cache(self):
        _, revision = self.previews.source("22.png")
        with patch.object(self.previews, "_render", wraps=self.previews._render) as render:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.previews.image("22", revision), range(8)))
            self.assertTrue(all(value == results[0] for value in results))
            self.assertEqual(render.call_count, 1)
        restarted = JacketPreviews(self.previews.directory, lambda: [self.catalog])
        with patch.object(restarted, "_render", side_effect=AssertionError("should reuse cache")):
            self.assertEqual(restarted.image("22", revision), results[0])

    def test_cache_eviction_is_bounded_and_does_not_touch_originals(self):
        previews = JacketPreviews(self.root / "small-cache", lambda: [self.catalog], max_files=1)
        _, first = previews.source("22.png")
        previews.image("22", first)
        Image.new("RGB", (400, 400), "orange").save(self.catalog / "jackets/30.png")
        _, second = previews.source("30.png")
        previews.image("30", second)
        self.assertEqual(len(list(previews.directory.glob("*.webp"))), 1)
        self.assertTrue(self.original.is_file())
        previews.image("22", first)
        self.assertEqual(len(list(previews.directory.glob("*.webp"))), 1)
        tiny = JacketPreviews(self.root / "tiny-cache", lambda: [self.catalog], max_bytes=1)
        self.assertTrue(tiny.image("22", first))
        self.assertEqual(list(tiny.directory.glob("*.webp")), [])

    def test_api_projection_preserves_original_and_keeps_personal_responses_uncached(self):
        self.login()
        url = self.previews.url("/jackets/22.png")
        paths = ("/api/charts?q=22", "/api/completion-table?q=22")
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.headers["cache-control"], "no-store")
            item = next(
                item for item in response.json()["items"] if item["chart_key"] == "22:standard:4"
            )
            self.assertEqual((item["thumbnail_url"], item["jacket_url"]), (url, "/jackets/22.png"))
        detail = self.client.get("/api/charts/22:standard:4").json()
        self.assertEqual(detail["thumbnail_url"], url)
        body = {"venue": "Fixture venue", "request_id": "thumbnail-test", "expected_revision": 0}
        self.assertEqual(
            self.client.put("/api/charts/22:standard:4/ap-record", json=body).status_code, 200
        )
        pending = self.client.get("/api/ap-records")
        self.assertEqual(pending.headers["cache-control"], "no-store")
        self.assertEqual(pending.json()["items"][0]["thumbnail_url"], url)
        self.assertEqual(self.client.get("/api/summary").headers["cache-control"], "no-store")
        self.assertEqual(
            self.client.get("/api/completion-exports/missing/image").headers["cache-control"],
            "no-store",
        )

    def test_only_artwork_and_real_hashed_bundles_receive_cache_headers(self):
        self.assertEqual(self.client.get("/").headers["cache-control"], "no-store")
        self.assertEqual(
            self.client.get("/assets/index-AbC12345.js").headers["cache-control"], IMMUTABLE_CACHE
        )
        self.assertEqual(self.client.get("/cover-assets/badge.png").status_code, 401)
        self.login()
        self.assertEqual(
            self.client.get("/cover-assets/badge.png").headers["cache-control"], REVALIDATE_CACHE
        )
        self.assertEqual(
            self.client.get("/cover-assets/manifest.json").headers["cache-control"], "no-store"
        )
        for path in (
            "/jackets/thumbnails/22/invalid.webp",
            "/jackets/thumbnails/invalid/000000000000000000000000.webp",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
