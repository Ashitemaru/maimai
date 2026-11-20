"""Exercise the backup capture and restic orchestration with a fake restic."""

import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from deploy.backup import BackupError, Backups, capture, verify

SNAPSHOT_ID = "c" * 64


def make_database(path, value="fixture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE fixture (value TEXT)")
        conn.execute("INSERT INTO fixture VALUES (?)", (value,))


def write(path, text="fixture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class FakeRestic:
    def __init__(self):
        self.calls = []
        self.fail = None

    def __call__(self, command, *, cwd=None, **_):
        action = command[7]
        self.calls.append((action, cwd))
        if action == self.fail:
            return subprocess.CompletedProcess(command, 1, "", "injected failure")
        if action == "backup":
            assert (Path(cwd) / "payload/manifest.json").is_file()
            summary = {"message_type": "summary", "snapshot_id": SNAPSHOT_ID}
            return subprocess.CompletedProcess(command, 0, json.dumps(summary) + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class BackupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="mai-backup-tests-")
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.root = self.base / "srv"
        make_database(self.root / "shared/runtime/personal.sqlite3", "personal")
        make_database(self.root / "shared/catalog/updates/adjudications.sqlite3", "decision")
        write(self.root / "shared/runtime/secrets.json", "{}")
        write(self.root / "shared/runtime/jacket-thumbnails/1.webp")
        catalog = self.root / "shared/catalog"
        for name in ("new", "old", "older"):
            make_database(catalog / "updates/snapshots" / name / "catalog.sqlite", name)
        write(catalog / "updates/status.json", json.dumps({"previous_snapshot": "old"}))
        write(catalog / "updates/publish.lock", "")
        write(catalog / "remy-cache/page.html")
        write(catalog / "jackets/1.png")
        (catalog / "current").symlink_to("updates/snapshots/new")
        write(self.root / "shared/assets/font.ttf")
        (self.root / "releases" / ("a" * 40)).mkdir(parents=True)
        (self.root / "current").symlink_to("releases/" + "a" * 40)
        self.system = self.base / "system"
        write(self.system / "etc/mai-journal/app.env", "KEY=value\n")
        write(self.system / "etc/systemd/system/mai-journal.service")
        write(self.system / "etc/systemd/system/other.service")

    def test_capture_copies_data_catalog_pair_and_config_only(self):
        payload = self.base / "payload"
        manifest = capture(self.root, payload, system_root=self.system)
        self.assertEqual(manifest["catalog_snapshots"], ["new", "old"])
        self.assertEqual(manifest["release_sha"], "a" * 40)
        snapshots = payload / "shared/catalog/updates/snapshots"
        self.assertEqual(sorted(p.name for p in snapshots.iterdir()), ["new", "old"])
        self.assertEqual(
            Path("updates/snapshots/new"), Path((payload / "shared/catalog/current").readlink())
        )
        self.assertTrue((payload / "shared/catalog/jackets/1.png").is_file())
        self.assertFalse((payload / "shared/catalog/remy-cache").exists())
        self.assertFalse((payload / "shared/runtime/jacket-thumbnails").exists())
        self.assertFalse((payload / "releases").exists())
        self.assertTrue((payload / "system/etc/mai-journal/app.env").is_file())
        units = payload / "system/etc/systemd/system"
        self.assertEqual([p.name for p in units.iterdir()], ["mai-journal.service"])
        self.assertEqual(verify(payload), manifest)

    def test_missing_required_database_fails_and_verify_rejects_corruption(self):
        payload = self.base / "payload"
        capture(self.root, payload, system_root=self.system)
        (payload / "shared/runtime/personal.sqlite3").write_bytes(b"not a database")
        with self.assertRaises(sqlite3.DatabaseError):
            verify(payload)
        (self.root / "shared/runtime/personal.sqlite3").unlink()
        with self.assertRaisesRegex(BackupError, "personal"):
            capture(self.root, self.base / "other", system_root=self.system)

    def test_backup_runs_restic_steps_and_records_status(self):
        restic = FakeRestic()
        directory = self.base / "backups"
        (directory / "repository").mkdir(parents=True)
        (directory / "repository/config").write_text("")
        backups = Backups(
            self.root, directory, self.base / "pw", runner=restic, system_root=self.system
        )
        backups.backup()
        self.assertEqual([c[0] for c in restic.calls], ["backup", "check", "forget"])
        status = json.loads((directory / "status.json").read_text())
        self.assertEqual((status["state"], status["snapshot_id"]), ("complete", SNAPSHOT_ID))
        self.assertFalse((directory / "staging").exists())
        restic.fail = "check"
        with self.assertRaisesRegex(BackupError, "restic check failed"):
            backups.backup()
        status = json.loads((directory / "status.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertIsNotNone(status["last_success"])
        self.assertFalse((directory / "staging").exists())

    def test_backup_requires_initialized_repository_and_restore_refuses_live_paths(self):
        backups = Backups(self.root, self.base / "backups", self.base / "pw", runner=FakeRestic())
        with self.assertRaisesRegex(BackupError, "init"):
            backups.backup()
        for destination in (self.root / "restore", self.base / "backups" / "restore", self.root):
            with self.subTest(destination=destination), self.assertRaises(BackupError):
                backups.restore("latest", destination)


if __name__ == "__main__":
    unittest.main()
