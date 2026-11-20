"""Configure isolated paths before any API test uses the application factory.

Works for direct ``python -m unittest tests.test_app`` as well as the runner.
All generated catalog, legacy imports, preferences, secrets and decisions live in
one temporary tree. The runner additionally denies network connections.

Also provides the shared fixtures: a fake catalog updater, a per-test copy of the
generated catalog with an API client, and a one-chart store for AP record tests.
"""

import atexit
import importlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from tests.fixtures.catalog import write_catalog, write_imports

_environment = tempfile.TemporaryDirectory(prefix="maimai-tests-")
ROOT = Path(_environment.name)
write_catalog(ROOT / "data/catalog")
IMPORTS = write_imports(ROOT / "imports")
(ROOT / "data/assets").mkdir()
os.environ["MAIMAI_RUNTIME_DIR"] = str(ROOT / "global-runtime")
os.environ["MAIMAI_IMPORT_DIR"] = str(IMPORTS)
os.environ["MAIMAI_CATALOG_DIR"] = str(ROOT / "data/catalog")
os.environ["MAIMAI_ASSETS_DIR"] = str(ROOT / "data/assets")
os.environ["MAIMAI_FRONTEND_DIR"] = str(ROOT / "frontend/dist")
os.environ["MAIMAI_ENV"] = "test"
os.environ["DIVINGFISH_IMPORT_TOKEN"] = ""

_store = importlib.import_module("backend.store")
_store.ROOT = ROOT
Store = _store.Store
_app = importlib.import_module("backend.app")
create_app = _app.create_app
atexit.register(_environment.cleanup)

FOREIGN = {"Origin": "https://external.example"}


class FakeUpdater:
    """Records catalog operations; set ``error`` to make the next one raise."""

    def __init__(self):
        self.calls = []
        self.error = None
        self.closed = False

    def status(self):
        return {"state": "idle", "calls": self.calls}

    def operation(self, name):
        if self.error:
            raise self.error
        self.calls.append(name)
        return {"state": "running", "operation": name}

    def start(self):
        return self.operation("update")

    def retry_remy(self):
        return self.operation("remy")

    def record_review(self):
        return {"records": [], "revision": "r1", "decision_revision": 0}

    manual_records = record_review

    def decide_records(self, value):
        self.last_records = value
        return self.operation("record-batch")

    def revoke_record(self, value):
        self.last_revoke = value
        return self.operation("record-revoke")

    def reapply_decisions(self):
        return self.operation("reapply")

    def close(self):
        self.closed = True


def copy_catalog(directory):
    """Private copy of the generated catalog that a test may rewrite."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "data/catalog/catalog.sqlite", directory / "catalog.sqlite")
    return directory


def add_venue(store, name):
    with store.db() as conn:
        conn.execute("INSERT OR IGNORE INTO venues VALUES(?,?,?)", (name, "fixture", ""))


def count(store, table):
    with store.db() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class FixtureAppCase(unittest.TestCase):
    """A store over a private generated catalog plus synthetic imports, and its client."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.catalog_dir = copy_catalog(self.root / "catalog")
        self.store = self.new_store()
        self.addCleanup(self.store.close)
        self.updater = FakeUpdater()
        self.client = TestClient(create_app(self.store, updater=self.updater))
        self.addCleanup(self.client.close)

    def new_store(self, catalog_dir=None):
        return Store(runtime=self.root / "runtime", catalog_dir=catalog_dir or self.catalog_dir)


def empty_personal_database(runtime):
    """A personal database that has already imported (nothing), so no TSVs are read."""
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(runtime / "personal.sqlite3")) as conn, conn:
        conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO metadata VALUES(?,?)", ("import", "{}"))
    return runtime


TINY_CHART = {
    "id": 10001,
    "difficulty": 3,
    "chart_key": "10001:3",
    "lxns_song_id": 1,
    "type": "dx",
    "title": "Example",
    "artist": "Artist",
    "difficulty_name": "MASTER",
    "constant": 13.3,
    "level": "13",
    "aliases": ["Nickname"],
    "jacket_path": "jackets/1.png",
    "external_ids": [{"namespace": "divingfish", "id": "10001", "type": "dx"}],
    "release_status": "resolved",
    "jp_release_version": "UNiVERSE",
    "admission_status": "admitted",
}


def write_tiny_catalog(directory):
    """One admitted DX MASTER chart; the source also lists a not-yet-admitted Re:MASTER."""
    (directory / "raw").mkdir(parents=True)
    with closing(sqlite3.connect(directory / "catalog.sqlite")) as conn, conn:
        conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value_json TEXT)")
        conn.execute("INSERT INTO metadata VALUES(?,?)", ("versions_cn", "[]"))
        conn.execute(
            "CREATE TABLE charts(id INTEGER,difficulty INTEGER,type TEXT,data_json TEXT,"
            "PRIMARY KEY(id,difficulty))"
        )
        conn.execute("INSERT INTO charts VALUES(?,?,?,?)", (10001, 3, "dx", json.dumps(TINY_CHART)))
    levels = [
        {"difficulty": 3, "level": "13", "level_value": 13.3},
        {"difficulty": 4, "level": "14", "level_value": 14.0},
    ]
    song = {"id": 1, "title": "Example", "artist": "Artist"}
    song["difficulties"] = {"standard": [], "dx": levels, "utage": []}
    (directory / "raw/lxns_songs.json").write_text(json.dumps({"songs": [song]}))
    return directory


class TinyStoreCase(unittest.TestCase):
    """Store over the one-chart catalog, with no legacy import, plus AP record helpers."""

    key = "10001:3"
    venue = "【测试市】测试机厅"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.catalog = write_tiny_catalog(self.root / "catalog")
        empty_personal_database(self.runtime)
        self.store = Store(runtime=self.runtime, catalog_dir=self.catalog)
        self.addCleanup(self.store.close)

    def client(self):
        client = TestClient(create_app(self.store, updater=FakeUpdater()))
        self.addCleanup(client.close)
        return client

    def save(self, *, venue=None, request_id="request-1", revision=0):
        venue = self.venue if venue is None else venue
        if venue.strip():
            add_venue(self.store, venue.strip())
        return self.store.save_ap_record(
            self.key, {"venue": venue, "request_id": request_id, "expected_revision": revision}
        )

    def score(self, fc="ap", **overrides):
        return {
            "song_id": 10001,
            "level_index": 3,
            "type": "DX",
            "fc": fc,
            "achievements": 100.9622,
            "dxScore": 1234,
            "fs": "fsp",
            "rate": "sssp",
            "ra": 299,
            **overrides,
        }

    def sync(self, fc="ap", **overrides):
        return self.store.import_provider({"records": [self.score(fc, **overrides)]})
