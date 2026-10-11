"""Single-owner deployment, credential races, and optimistic manual edits."""

import importlib
import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.store import Store
from backend.test_app import FakeUpdater
from tests.fixtures.catalog import write_catalog
from tests.support import create_app


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = write_catalog(self.root / "catalog")
        self.store = self.new_store()
        self.app = create_app(self.store, updater=FakeUpdater())
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def new_store(self):
        return Store(runtime=self.root / "runtime", catalog_dir=self.catalog)

    def response(self, fc="ap"):
        return Mock(
            status_code=200,
            json=lambda: {
                "records": [{"song_id": 30, "level_index": 0, "fc": fc, "achievements": 100.5}]
            },
        )

    def test_parallel_token_saves_are_atomic_private_and_leave_no_temporary_files(self):
        barrier = threading.Barrier(8)
        tokens = [f"synthetic-token-{index}-" + "x" * (200 + index) for index in range(8)]

        def save(token):
            barrier.wait(timeout=5)
            self.store.save_token(token)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(save, tokens))
        stored = json.loads(self.store.secret_path.read_text())
        self.assertIn(stored["divingfish_import_token"], tokens)
        self.assertEqual(stat.S_IMODE(self.store.secret_path.stat().st_mode), 0o600)
        self.assertEqual(list(self.store.runtime.glob(".secrets*.tmp")), [])
        self.assertEqual(self.store.token()[0], stored["divingfish_import_token"])

    def test_failed_token_replace_preserves_previous_file_and_cleans_temp(self):
        self.store.save_token("synthetic-before")
        before = self.store.secret_path.read_bytes()
        with (
            patch.object(Path, "replace", side_effect=OSError("disk failure")),
            self.assertRaises(OSError),
        ):
            self.store.save_token("synthetic-after")
        self.assertEqual(self.store.secret_path.read_bytes(), before)
        self.assertEqual(list(self.store.runtime.glob(".secrets*.tmp")), [])

    def test_corrupt_credential_is_reported_without_echo_and_can_be_replaced(self):
        self.store.secret_path.write_text('{"private-synthetic-corruption"')
        response = self.client.get("/api/settings/divingfish")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["configured"])
        self.assertEqual(response.json()["source"], "invalid")
        self.assertNotIn("private-synthetic", response.text)
        self.store.save_token("replacement")
        self.assertTrue(self.client.get("/api/settings/divingfish").json()["configured"])

    def test_sync_lease_rejects_overlap_and_is_released_after_completion(self):
        self.store.save_token("synthetic")
        entered, release = threading.Event(), threading.Event()

        def fetch(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release provider response")
            return self.response("fc")

        with (
            patch("backend.app.httpx.get", side_effect=fetch) as upstream,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            request = pool.submit(self.client.post, "/api/sync/divingfish")
            try:
                self.assertTrue(entered.wait(timeout=5))
                blocked = self.client.post("/api/sync/divingfish")
                self.assertEqual(blocked.status_code, 409)
                self.assertEqual(blocked.json()["detail"]["code"], "sync_busy")
                self.assertEqual(upstream.call_count, 1)
            finally:
                release.set()
            self.assertEqual(request.result(timeout=5).status_code, 200)
        with patch("backend.app.httpx.get", return_value=self.response()):
            self.assertEqual(self.client.post("/api/sync/divingfish").status_code, 200)
        self.assertTrue(self.store.chart("30:standard:0")["is_ap"])

    def test_token_change_invalidates_inflight_response_without_touching_scores(self):
        self.store.save_token("synthetic-old")
        self.store.import_provider(self.response().json())
        before = self.store.meta("last_sync")
        entered, release = threading.Event(), threading.Event()

        def fetch(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release provider response")
            return self.response("fc")

        with (
            patch("backend.app.httpx.get", side_effect=fetch),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            request = pool.submit(self.client.post, "/api/sync/divingfish")
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.assertEqual(
                    self.client.put(
                        "/api/settings/divingfish", json={"token": "synthetic-new"}
                    ).status_code,
                    200,
                )
            finally:
                release.set()
            result = request.result(timeout=5)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["detail"]["code"], "token_changed")
        self.assertTrue(self.store.chart("30:standard:0")["is_ap"])
        self.assertEqual(self.store.meta("last_sync"), before)
        with patch("backend.app.httpx.get", return_value=self.response()):
            self.assertEqual(self.client.post("/api/sync/divingfish").status_code, 200)

    def test_sync_failure_releases_lease(self):
        self.store.save_token("synthetic")
        with patch("backend.app.httpx.get", return_value=Mock(status_code=500)):
            self.assertEqual(self.client.post("/api/sync/divingfish").status_code, 502)
        with patch("backend.app.httpx.get", return_value=self.response()):
            self.assertEqual(self.client.post("/api/sync/divingfish").status_code, 200)

    def test_stale_manual_put_and_delete_cannot_overwrite_newer_record(self):
        path = "/api/charts/30:standard:0/manual"
        first = self.client.put(path, json={"expected_revision": 0, "note": "newer"}).json()[
            "chart"
        ]
        for response in (
            self.client.put(path, json={"expected_revision": 0, "note": "stale"}),
            self.client.delete(path, params={"expected_revision": 0}),
        ):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "manual_conflict")
            self.assertEqual(response.json()["detail"]["chart"]["note"], "newer")
        self.assertGreater(first["manual_revision"], 0)
        restored = self.client.delete(
            path, params={"expected_revision": first["manual_revision"]}
        ).json()["chart"]
        self.assertGreater(restored["manual_revision"], first["manual_revision"])
        self.assertIsNone(restored["manual_ap"])
        self.assertEqual(
            self.new_store().chart("30:standard:0")["manual_revision"], restored["manual_revision"]
        )
        self.assertEqual(self.client.put(path, json={"expected_revision": 0}).status_code, 409)

    def test_parallel_manual_saves_commit_exactly_one_revision_and_event(self):
        path = "/api/charts/30:standard:0/manual"
        barrier = threading.Barrier(2)

        def save(note):
            barrier.wait(timeout=5)
            return self.client.put(path, json={"expected_revision": 0, "note": note})

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(save, ("first", "second")))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        winner = next(
            response.json()["chart"] for response in responses if response.status_code == 200
        )
        self.assertEqual(self.store.chart("30:standard:0")["note"], winner["note"])
        with self.store.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events WHERE kind='manual'").fetchone()[0], 1
            )

    def test_manual_revision_precondition_is_required_and_strict(self):
        path = "/api/charts/30:standard:0/manual"
        for payload in (
            {},
            {"expected_revision": -1},
            {"expected_revision": True},
            {"expected_revision": "0"},
        ):
            self.assertEqual(self.client.put(path, json=payload).status_code, 422)
        self.assertEqual(self.client.delete(path).status_code, 422)

    def test_readiness_detects_missing_databases_while_health_remains_live(self):
        with patch.dict(os.environ, {"MAIMAI_RELEASE_SHA": "synthetic-release"}):
            self.assertEqual(
                self.client.get("/api/health").json(),
                {"ok": True, "release_sha": "synthetic-release"},
            )
            response = self.client.get("/api/ready")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["checks"], {"catalog": True, "personal": True})
            self.assertEqual(response.headers["cache-control"], "no-store")
            for name, path in (
                ("catalog", self.catalog / "catalog.sqlite"),
                ("personal", self.store.db_path),
            ):
                hidden = path.with_suffix(".hidden")
                path.replace(hidden)
                try:
                    response = self.client.get("/api/ready")
                    self.assertEqual(response.status_code, 503)
                    self.assertFalse(response.json()["checks"][name])
                    self.assertEqual(self.client.get("/api/health").status_code, 200)
                    self.assertFalse(path.exists())
                finally:
                    hidden.replace(path)

    def test_production_uses_configured_paths_and_rejects_missing_files(self):
        assets, frontend = self.root / "assets", self.root / "frontend"
        assets.mkdir()
        frontend.mkdir()
        (assets / "fixture.txt").write_text("fixture asset")
        (frontend / "index.html").write_text("fixture frontend")
        environment = {
            "MAIMAI_ENV": "production",
            "MAIMAI_CATALOG_DIR": str(self.catalog),
            "MAIMAI_RUNTIME_DIR": str(self.store.runtime),
            "MAIMAI_ASSETS_DIR": str(assets),
            "MAIMAI_FRONTEND_DIR": str(frontend),
        }
        with patch.dict(os.environ, environment):
            rebuilt = Store()
            with TestClient(create_app(rebuilt, updater=FakeUpdater())) as client:
                self.assertEqual(client.get("/").text, "fixture frontend")
                self.assertEqual(client.get("/cover-assets/fixture.txt").text, "fixture asset")
            with patch.dict(os.environ, {"MAIMAI_RUNTIME_DIR": str(self.root / "missing-runtime")}):
                with self.assertRaisesRegex(RuntimeError, "personal.sqlite3 is missing"):
                    Store()
                self.assertFalse((self.root / "missing-runtime").exists())
            with patch.dict(os.environ, {"MAIMAI_CATALOG_DIR": str(self.root / "missing-catalog")}):
                with self.assertRaisesRegex(RuntimeError, "catalog.sqlite is missing"):
                    Store()
            (frontend / "index.html").unlink()
            with self.assertRaisesRegex(RuntimeError, "frontend/index.html is missing"):
                create_app(rebuilt, updater=FakeUpdater())

    def test_importing_application_module_does_not_initialize_store(self):
        import backend.app as module

        with patch(
            "backend.store.Store", side_effect=AssertionError("Store initialized during import")
        ):
            importlib.reload(module)
        importlib.reload(module)
