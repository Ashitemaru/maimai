import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from deploy.backup_data import (
    LEGACY_OPTIONAL_PATHS,
    WAHLAP_PROXY_PATHS,
    WAHLAP_PROXY_SYSTEM_PATHS,
    BackupDataError,
    capture,
    verify,
)

SHA = "a" * 40


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)


def document(path, data):
    write(path, json.dumps(data))


def database(path, tables):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        for table, values in tables.items():
            connection.execute(f'CREATE TABLE "{table}" (value TEXT)')
            connection.executemany(f'INSERT INTO "{table}" VALUES (?)', [(x,) for x in values])


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BackupDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "application"
        self.config = self.base / "configuration"
        self.system = self.base / "system"
        self.destination = self.base / "backup"
        self.catalog = self.source / "shared/catalog"
        self.runtime = self.source / "shared/runtime"
        database(
            self.runtime / "personal.sqlite3",
            {
                "ap_confirmations": ["recorded"],
                "ap_pending": ["waiting", "second"],
                "venues": ["test venue"],
            },
        )
        database(self.runtime / "auth.sqlite3", {"sessions": ["digest-only"]})
        database(
            self.catalog / "updates/adjudications.sqlite3",
            {"decisions": ["saved-not-published"], "events": ["first", "second"]},
        )
        document(self.runtime / "secrets.json", {"fixture": "private"})
        write(self.runtime / "jacket-thumbnails/cached.webp", "not a durable input")
        write(self.runtime / "wahlap-worker/prefix/transient", "not a durable input")
        write(self.runtime / "completion-exports/test.png", "not a durable input")
        write(self.runtime / ".score-sync.lock", "")
        for ident in ("current-snapshot", "previous-snapshot", "older-snapshot"):
            directory = self.catalog / "updates/snapshots" / ident
            database(directory / "catalog.sqlite", {"charts": [ident]})
            document(directory / "catalog.json", {"schema_version": 2, "decision_revision": 2})
            document(directory / "catalog_adjudications.json", {"revision": 2, "decisions": []})
            document(directory / "snapshot.json", {"id": ident, "parent": None})
            write(directory / "raw/lxns_songs.json", '{"songs":[]}')
            write(directory / "remy-evidence/proof.html", "source evidence")
            write(directory / "jackets/1.png", "fixture jacket")
        document(
            self.catalog / "updates/status.json",
            {
                "previous_snapshot": "previous-snapshot",
                "history": [{"id": "older-snapshot"}],
            },
        )
        (self.catalog / "current").symlink_to("updates/snapshots/current-snapshot")
        write(self.catalog / "jackets/fallback.png", "fallback source")
        write(self.catalog / "raw/fallback.json", "{}")
        write(self.catalog / "updates/publish.lock", "")
        write(self.catalog / "updates/staging/interrupted/partial", "discard")
        write(self.catalog / "remy-cache/fetch.html", "discard")
        write(self.catalog / "incomplete.tmp", "discard")
        write(self.source / "shared/assets/background.png", "fixture artwork")
        release = self.source / "releases" / SHA
        write(release / "RELEASE_SHA", SHA + "\n")
        write(release / "requirements.lock", "fixture==1.0.0\n")
        write(release / "release.env", f"MAIMAI_RELEASE_SHA={SHA}\n")
        write(release / "backend/app.py", "# fixture code\n")
        write(release / "frontend/dist/index.html", "<title>fixture</title>")
        write(release / "venv/bin/python", "omit interpreter")
        write(release / "backend/__pycache__/app.pyc", "omit cache")
        (self.source / "current").symlink_to(f"releases/{SHA}")
        write(self.source / "deploy.lock", "")
        write(self.config / "app.env", "FIXTURE_KEY_HASH=redacted\n")
        write(self.config / "deploy.env", "MAIMAI_REPOSITORY=fixture/repo\n")
        write(self.config / "backup-password", "must not be backed up")
        write(self.system / "etc/caddy/Caddyfile", "fixture proxy")
        write(self.system / "etc/sudoers.d/mai-journal", "fixture sudoers")
        for name in (
            "mai-journal.service",
            "mai-journal-deploy.timer",
            "mai-journal-wahlap@.service",
            "mai-journal-wahlap.socket",
        ):
            write(self.system / "etc/systemd/system" / name, "fixture unit")
        write(self.system / "etc/systemd/system/other.service", "not app config")

    def capture(self):
        return capture(self.source, self.config, self.destination, system_root=self.system)

    def save_manifest(self, manifest):
        document(self.destination / "manifest.json", manifest)

    def test_capture_keeps_durable_inputs_byte_exact_catalog_and_drops_disposable_data(self):
        before = {
            path.relative_to(self.source).as_posix(): digest(path)
            for path in self.source.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        manifest = self.capture()
        self.assertEqual(manifest, verify(self.destination))
        self.assertEqual(manifest["release_sha"], SHA)
        self.assertEqual(
            manifest["catalog"]["snapshots"], ["current-snapshot", "previous-snapshot"]
        )
        self.assertEqual(
            manifest["databases"]["shared/runtime/personal.sqlite3"]["tables"],
            {"ap_confirmations": 1, "ap_pending": 2, "venues": 1},
        )
        self.assertEqual(
            os.readlink(self.destination / "shared/catalog/current"),
            "updates/snapshots/current-snapshot",
        )
        for relative in (
            "shared/catalog/updates/snapshots/current-snapshot/catalog.sqlite",
            "shared/catalog/updates/snapshots/current-snapshot/remy-evidence/proof.html",
            "shared/catalog/updates/snapshots/current-snapshot/raw/lxns_songs.json",
            "shared/catalog/jackets/fallback.png",
            "shared/assets/background.png",
        ):
            self.assertEqual(digest(self.destination / relative), before[relative])
        excluded = (
            "older-snapshot",
            "staging",
            "remy-cache",
            "incomplete.tmp",
            "jacket-thumbnails",
            "completion-exports",
            "wahlap-worker",
            "__pycache__",
            "venv",
            "publish.lock",
            "deploy.lock",
            ".score-sync.lock",
            "backup-password",
            "other.service",
        )
        self.assertFalse(any(part in path for part in excluded for path in manifest["entries"]))
        self.assertIn(f"releases/{SHA}/requirements.lock", manifest["entries"])
        for relative, expected in before.items():
            self.assertEqual(digest(self.source / relative), expected)
        self.assertEqual((self.destination.stat().st_mode & 0o777), 0o700)
        self.assertEqual(
            ((self.destination / "shared/runtime/personal.sqlite3").stat().st_mode & 0o777), 0o600
        )
        # Saved decisions may legitimately be newer than the published catalog; no rewrite occurs.
        with closing(
            sqlite3.connect(self.destination / "shared/catalog/updates/adjudications.sqlite3")
        ) as connection:
            self.assertEqual(
                connection.execute("SELECT value FROM decisions").fetchone()[0],
                "saved-not-published",
            )

    def test_auth_and_secret_are_optional_but_absence_is_recorded(self):
        (self.runtime / "auth.sqlite3").unlink()
        (self.runtime / "secrets.json").unlink()
        manifest = self.capture()
        self.assertEqual(
            manifest["optional_absent"],
            sorted(
                ["shared/runtime/auth.sqlite3", "shared/runtime/secrets.json", *WAHLAP_PROXY_PATHS]
            ),
        )

    def add_proxy_configuration(self):
        for relative in WAHLAP_PROXY_SYSTEM_PATHS:
            source = self.system / relative
            write(source, "private-fixture-proxy-material")
            source.chmod(0o640)
        (self.system / "etc/mai-journal-wahlap-proxy").chmod(0o750)

    def test_proxy_recovery_files_are_private_exact_and_exclude_neighboring_secrets(self):
        self.add_proxy_configuration()
        for relative in (
            "etc/mai-journal-wahlap-proxy/candidate-node.json",
            "etc/mai-journal-wahlap-proxy/egress.json.old",
            "etc/netns/other/resolv.conf",
            "etc/systemd/system/mai-journal-wahlap@.service.d/unrelated.conf",
        ):
            write(self.system / relative, "unrelated-secret-must-not-copy")
        manifest = self.capture()
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(verify(self.destination), manifest)
        self.assertFalse(any(path in manifest["optional_absent"] for path in WAHLAP_PROXY_PATHS))
        for source_path, saved_path in zip(
            WAHLAP_PROXY_SYSTEM_PATHS, WAHLAP_PROXY_PATHS, strict=True
        ):
            source, saved = self.system / source_path, self.destination / saved_path
            self.assertEqual(digest(source), digest(saved))
            self.assertEqual(source.stat().st_mode & 0o777, 0o640)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (self.system / "etc/mai-journal-wahlap-proxy").stat().st_mode & 0o777, 0o750
        )
        self.assertFalse(
            any(
                "candidate-node" in path or "unrelated" in path or path.endswith(".old")
                for path in manifest["entries"]
            )
        )
        self.assertNotIn("private-fixture-proxy-material", json.dumps(manifest))
        (self.destination / WAHLAP_PROXY_PATHS[-1]).unlink()
        with self.assertRaisesRegex(BackupDataError, "inventory"):
            verify(self.destination)

    def test_missing_proxy_dropin_cannot_be_reported_as_complete_recovery(self):
        self.add_proxy_configuration()
        (self.system / WAHLAP_PROXY_SYSTEM_PATHS[-1]).unlink()
        with self.assertRaisesRegex(BackupDataError, "proxy recovery files are incomplete"):
            self.capture()

    def test_proxy_configuration_parent_symlink_is_not_followed(self):
        self.add_proxy_configuration()
        directory = self.system / "etc/mai-journal-wahlap-proxy"
        directory.rename(self.base / "outside-proxy")
        directory.symlink_to(self.base / "outside-proxy")
        with self.assertRaisesRegex(BackupDataError, "symbolic link"):
            self.capture()

    def test_previous_schema_one_backups_remain_verifiable(self):
        manifest = self.capture()
        manifest["schema_version"] = 1
        manifest["optional_absent"] = sorted(
            path for path in LEGACY_OPTIONAL_PATHS if path not in manifest["entries"]
        )
        self.save_manifest(manifest)
        self.assertEqual(verify(self.destination), manifest)

    def test_sqlite_backup_includes_committed_wal_rows_without_copying_sidecars(self):
        source = self.runtime / "personal.sqlite3"
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("INSERT INTO ap_pending VALUES ('committed in WAL')")
            connection.commit()
            self.assertTrue(source.with_name("personal.sqlite3-wal").is_file())
            manifest = self.capture()
            self.assertEqual(
                manifest["databases"]["shared/runtime/personal.sqlite3"]["tables"]["ap_pending"], 3
            )
            self.assertFalse(any(path.endswith(("-wal", "-shm")) for path in manifest["entries"]))

    def test_verify_rejects_false_optional_absence_receipt(self):
        manifest = self.capture()
        manifest["optional_absent"] = ["shared/runtime/auth.sqlite3"]
        self.save_manifest(manifest)
        with self.assertRaisesRegex(BackupDataError, "Optional"):
            verify(self.destination)

    def test_legacy_root_catalog_without_pointer_is_preserved(self):
        (self.catalog / "current").unlink()
        directory = self.catalog / "updates/snapshots/current-snapshot"
        for name in ("catalog.sqlite", "catalog.json"):
            (self.catalog / name).write_bytes((directory / name).read_bytes())
        document(self.catalog / "updates/status.json", {"previous_snapshot": None})
        manifest = self.capture()
        self.assertEqual(manifest["catalog"]["current"], "shared/catalog")
        self.assertEqual(manifest["catalog"]["snapshots"], [])
        self.assertFalse((self.destination / "shared/catalog/current").exists())

    def test_missing_required_database_fails_without_modifying_source(self):
        for relative in (
            "shared/runtime/personal.sqlite3",
            "shared/catalog/updates/adjudications.sqlite3",
        ):
            with self.subTest(relative=relative):
                path = self.source / relative
                original = path.read_bytes()
                path.unlink()
                self.destination = self.base / ("missing-" + path.name)
                with self.assertRaises(BackupDataError):
                    self.capture()
                self.assertFalse((self.destination / "manifest.json").exists())
                path.write_bytes(original)

    def test_interrupted_deployment_is_not_a_valid_capture_baseline(self):
        document(self.source / "deployment-pending.json", {"target": SHA})
        with self.assertRaisesRegex(BackupDataError, "interrupted deployment"):
            self.capture()

    def test_nonempty_or_input_overlapping_destination_is_rejected(self):
        self.destination.mkdir()
        write(self.destination / "existing", "never overwrite")
        with self.assertRaisesRegex(BackupDataError, "empty"):
            self.capture()
        self.assertEqual((self.destination / "existing").read_text(), "never overwrite")
        with self.assertRaisesRegex(BackupDataError, "overlaps"):
            capture(self.source, self.config, self.source / "capture", system_root=self.system)

    def test_input_symlinks_outside_controlled_pointers_are_rejected(self):
        (self.source / "shared/assets/stolen").symlink_to(self.config / "backup-password")
        with self.assertRaisesRegex(BackupDataError, "symbolic link"):
            self.capture()

    def test_symlinked_source_parent_is_rejected(self):
        assets = self.source / "shared/assets"
        assets.rename(self.base / "outside-assets")
        assets.symlink_to(self.base / "outside-assets")
        with self.assertRaisesRegex(BackupDataError, "symbolic link"):
            self.capture()

    def test_absolute_and_traversing_snapshot_pointers_are_rejected(self):
        pointer = self.catalog / "current"
        for value in (
            str(self.catalog / "updates/snapshots/current-snapshot"),
            "updates/snapshots/../snapshots/current-snapshot",
        ):
            with self.subTest(value=value):
                pointer.unlink()
                pointer.symlink_to(value)
                self.destination = self.base / ("invalid-" + str(len(value)))
                with self.assertRaises(BackupDataError):
                    self.capture()

    def test_absolute_code_release_pointer_inside_source_is_normalized(self):
        pointer = self.source / "current"
        pointer.unlink()
        pointer.symlink_to(self.source / "releases" / SHA)
        self.capture()
        self.assertEqual(os.readlink(self.destination / "current"), f"releases/{SHA}")
        self.assertTrue(pointer.is_absolute())
        self.assertTrue(Path(os.readlink(pointer)).is_absolute())

    def test_absolute_code_release_pointer_outside_source_is_rejected(self):
        pointer = self.source / "current"
        pointer.unlink()
        pointer.symlink_to(self.base / "elsewhere/releases" / SHA)
        with self.assertRaisesRegex(BackupDataError, "escapes"):
            self.capture()

    def test_verify_requires_reinstallable_release_even_with_rewritten_inventory(self):
        manifest = self.capture()
        path = f"releases/{SHA}/requirements.lock"
        (self.destination / path).unlink()
        del manifest["entries"][path]
        self.save_manifest(manifest)
        with self.assertRaisesRegex(BackupDataError, "missing"):
            verify(self.destination)

    def test_bad_or_missing_previous_snapshot_is_rejected(self):
        for previous in ("../../secret", "not-present"):
            with self.subTest(previous=previous):
                document(self.catalog / "updates/status.json", {"previous_snapshot": previous})
                self.destination = self.base / ("bad-previous-" + str(len(previous)))
                with self.assertRaises(BackupDataError):
                    self.capture()

    def test_corrupt_database_is_rejected(self):
        (self.runtime / "personal.sqlite3").write_bytes(b"not sqlite")
        with self.assertRaises((BackupDataError, sqlite3.DatabaseError)):
            self.capture()
        self.assertFalse((self.destination / "manifest.json").exists())

    def test_verify_rejects_modified_missing_and_extra_files(self):
        self.capture()
        path = self.destination / "shared/assets/background.png"
        data = path.read_bytes()
        path.write_bytes(b"modified")
        with self.assertRaisesRegex(BackupDataError, "checksum"):
            verify(self.destination)
        path.unlink()
        with self.assertRaisesRegex(BackupDataError, "checksum"):
            verify(self.destination)
        path.write_bytes(data)
        path.chmod(0o600)
        write(self.destination / "extra", "unexpected")
        with self.assertRaisesRegex(BackupDataError, "checksum"):
            verify(self.destination)

    def test_verify_rejects_forged_path_and_escaping_symlink(self):
        manifest = self.capture()
        manifest["entries"]["../outside"] = {"type": "file", "bytes": 0, "sha256": ""}
        self.save_manifest(manifest)
        with self.assertRaisesRegex(BackupDataError, "path"):
            verify(self.destination)
        del manifest["entries"]["../outside"]
        pointer = self.destination / "current"
        pointer.unlink()
        pointer.symlink_to("../application/current")
        manifest["entries"]["current"]["target"] = "../application/current"
        self.save_manifest(manifest)
        with self.assertRaisesRegex(BackupDataError, "path"):
            verify(self.destination)

    def test_verify_requires_all_database_receipts_even_if_file_inventory_is_valid(self):
        manifest = self.capture()
        del manifest["databases"]["shared/runtime/personal.sqlite3"]
        self.save_manifest(manifest)
        with self.assertRaisesRegex(BackupDataError, "database receipt"):
            verify(self.destination)

    def test_verify_database_integrity_even_if_attacker_updates_file_hash(self):
        manifest = self.capture()
        path = self.destination / "shared/runtime/personal.sqlite3"
        path.write_bytes(b"corrupted sqlite")
        entry = manifest["entries"]["shared/runtime/personal.sqlite3"]
        entry.update(sha256=digest(path), bytes=path.stat().st_size)
        self.save_manifest(manifest)
        with self.assertRaises((BackupDataError, sqlite3.DatabaseError)):
            verify(self.destination)

    def test_verify_rejects_duplicate_manifest_json_keys(self):
        self.capture()
        (self.destination / "manifest.json").write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(BackupDataError, "Duplicate"):
            verify(self.destination)


if __name__ == "__main__":
    unittest.main()
