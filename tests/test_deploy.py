import contextlib
import fcntl
import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.deploy_release import (
    Deployer,
    DeployError,
    atomic_json,
    extract_archive,
    switch_current,
)
from tests.dependency_fixtures import dependency_files

OLD = "a" * 40
NEW = "b" * 40
OTHER = "c" * 40


def archive_bytes(sha=NEW, additions=(), replacements=None, omit=()):
    contents = {
        "RELEASE_SHA": sha + "\n",
        "requirements.lock": "example==1.0.0\n",
        "backend/app.py": "# Synthetic application fixture\n",
        "backend/store.py": "# Synthetic store fixture\n",
        "scripts/catalog_pipeline.py": "# Synthetic catalog fixture\n",
        "frontend/dist/index.html": "<!doctype html><title>Fixture</title>",
    }
    contents.update(dependency_files())
    contents.update(replacements or {})
    for name in omit:
        contents.pop(name)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, value in contents.items():
            body = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(body)
            tar.addfile(member, io.BytesIO(body))
        for item in additions:
            tar.addfile(item, io.BytesIO(b"x" * item.size) if item.isfile() else None)
    return buffer.getvalue()


class FakeHost:
    def __init__(self, root):
        self.root = root
        self.events = []
        self.failed_ready = set()
        self.fail_install = False
        self.after_install = None
        self.after_start = None
        self.stop_count = 0
        self.fail_stop_number = None

    def install(self, release):
        self.events.append(("install", release.name))
        if self.fail_install:
            raise DeployError("Injected install failure")
        (release / "venv/bin").mkdir(parents=True)
        (release / "venv/bin/python").write_text("fixture")
        if self.after_install:
            self.after_install()

    def service(self, action):
        current = self.root / "current"
        sha = current.resolve().name if current.is_symlink() else None
        self.events.append((action, sha))
        if action == "stop":
            self.stop_count += 1
            if self.stop_count == self.fail_stop_number:
                raise DeployError("Injected stop failure")
        if action == "start" and self.after_start:
            self.after_start(sha)

    def ready(self, sha):
        self.events.append(("ready", sha))
        return sha not in self.failed_ready


def make_database(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE fixture (value TEXT)")
        conn.execute("INSERT INTO fixture VALUES (?)", (value,))


def database_value(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT value FROM fixture").fetchone()[0]


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def extract(self, data):
        archive = self.root / "release.tar.gz"
        archive.write_bytes(data)
        target = self.root / "extracted"
        target.mkdir()
        extract_archive(archive, target, NEW)
        return target

    def test_valid_archive_has_matching_code_and_offline_dependencies(self):
        result = self.extract(archive_bytes())
        self.assertEqual((result / "RELEASE_SHA").read_text().strip(), NEW)
        self.assertFalse((result / "backend/runtime").exists())

    def test_reject_traversal_absolute_private_and_duplicate_members(self):
        for name in (
            "../outside",
            "/outside",
            "backend/runtime/personal.sqlite3",
            "deploy/app.env",
            "deploy/private.sqlite3",
            "deploy/runtime.json",
            "deploy/core.dat",
            "deploy/__pycache__/code.pyc",
            "data/catalog/catalog.sqlite",
            "frontend/src/secret.ts",
            "backend/app.py",
            "backend\\escape.py",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "release.tar.gz"
                archive.write_bytes(archive_bytes(additions=[tarfile.TarInfo(name)]))
                target = Path(temporary) / "code"
                target.mkdir()
                with self.assertRaises(DeployError):
                    extract_archive(archive, target, NEW)
                self.assertFalse((Path(temporary).parent / "outside").exists())

    def test_reject_links_devices_and_oversized_archive(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                item = tarfile.TarInfo("backend/link.py")
                item.type, item.linkname = kind, "../../outside"
                archive = Path(temporary) / "release.tar.gz"
                archive.write_bytes(archive_bytes(additions=[item]))
                target = Path(temporary) / "code"
                target.mkdir()
                with self.assertRaises(DeployError):
                    extract_archive(archive, target, NEW)
        with patch("deploy.deploy_release.MAX_EXTRACTED", 20), self.assertRaises(DeployError):
            self.extract(archive_bytes())

    def test_reject_mismatched_sha_and_non_pinned_requirements(self):
        with self.assertRaisesRegex(DeployError, "RELEASE_SHA"):
            self.extract(archive_bytes(OLD))
        for requirement in (
            "--extra-index-url https://example.test",
            "example>=1",
            "example @ https://example.test/pkg.whl",
        ):
            with self.subTest(requirement=requirement), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "release.tar.gz"
                archive.write_bytes(archive_bytes(replacements={"requirements.lock": requirement}))
                target = Path(temporary) / "code"
                target.mkdir()
                with self.assertRaisesRegex(DeployError, "Requirements"):
                    extract_archive(archive, target, NEW)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.old = self.root / "releases" / OLD
        self.old.mkdir(parents=True)
        (self.old / "RELEASE_SHA").write_text(OLD)
        switch_current(self.root, OLD)
        self.personal = self.root / "shared/runtime/personal.sqlite3"
        self.decisions = self.root / "shared/catalog/updates/adjudications.sqlite3"
        make_database(self.personal, "before")
        make_database(self.decisions, "permanent decision")
        self.host = FakeHost(self.root)
        self.deployer = Deployer(self.root, self.host)
        self.upload()

    def upload(self, sha=NEW, data=None):
        incoming = self.root / "incoming"
        incoming.mkdir(exist_ok=True)
        for old in incoming.iterdir():
            old.unlink()
        (incoming / f"mai-journal-{sha}.tar.gz").write_bytes(data or archive_bytes(sha))

    def test_success_stops_then_starts_exact_sha(self):
        self.assertEqual(self.deployer.run(), "deployed")
        self.assertEqual(
            self.host.events, [("install", NEW), ("stop", OLD), ("start", NEW), ("ready", NEW)]
        )
        self.assertEqual(self.deployer.installed(), NEW)
        self.assertEqual(
            (self.root / "current/release.env").read_text(), f"MAIMAI_RELEASE_SHA={NEW}\n"
        )
        self.assertFalse(self.deployer.journal.exists())

    def test_same_sha_or_missing_upload_do_not_stop_or_install(self):
        self.upload(OLD)
        self.assertEqual(self.deployer.run(), "unchanged")
        self.assertEqual(list((self.root / "incoming").iterdir()), [])
        self.assertEqual(self.deployer.run(), "pending")
        self.assertEqual(self.host.events, [])

    def test_failed_prepare_preserves_running_app_and_consumes_upload(self):
        self.host.fail_install = True
        with self.assertRaises(DeployError):
            self.deployer.run()
        self.assertEqual(self.deployer.installed(), OLD)
        self.assertFalse((self.root / "releases" / NEW).exists())
        self.assertEqual(self.deployer.run(), "pending")
        self.host.fail_install = False
        self.upload()
        self.assertEqual(self.deployer.run(), "deployed")

    def test_cached_wheels_fill_an_upload_without_dependencies(self):
        self.assertEqual(self.deployer.run(), "deployed")
        manifest = json.loads(dependency_files()["dependencies/manifest.json"])
        cached = sorted(p.name for p in (self.root / "wheels").iterdir())
        self.assertEqual(cached, sorted(f"{w['sha256']}.whl" for w in manifest["wheels"]))
        slim = [name for name in dependency_files() if name != "dependencies/manifest.json"]
        self.upload(OTHER, archive_bytes(OTHER, omit=slim))
        self.assertEqual(self.deployer.run(), "deployed")
        self.assertEqual(self.deployer.installed(), OTHER)
        (self.root / "wheels" / cached[0]).unlink()
        self.upload(NEW, archive_bytes(NEW, omit=slim))
        with self.assertRaisesRegex(DeployError, "Missing or unsafe dependency wheel"):
            self.deployer.run()
        self.assertEqual(self.deployer.installed(), OTHER)

    def test_success_prunes_old_releases(self):
        for name in (OTHER, "d" * 40):
            (self.root / "releases" / name).mkdir()
        self.assertEqual(self.deployer.run(), "deployed")
        self.assertEqual(sorted(p.name for p in (self.root / "releases").iterdir()), [OLD, NEW])

    def test_multiple_uploads_are_not_deployed(self):
        (self.root / "incoming" / f"mai-journal-{OTHER}.tar.gz").write_bytes(b"x")
        self.assertEqual(self.deployer.run(), "pending")
        self.assertEqual(self.host.events, [])

    def test_health_failure_rolls_back_code_without_overwriting_new_personal_data(self):
        self.host.failed_ready.add(NEW)

        def write_latest(sha):
            if sha == NEW:
                with contextlib.closing(sqlite3.connect(self.personal)) as conn, conn:
                    conn.execute("UPDATE fixture SET value='written by new app'")

        self.host.after_start = write_latest
        with self.assertRaisesRegex(DeployError, "did not become ready"):
            self.deployer.run()
        self.assertEqual(self.deployer.installed(), OLD)
        self.assertEqual(database_value(self.personal), "written by new app")
        self.assertEqual(self.host.events[-3:], [("stop", NEW), ("start", OLD), ("ready", OLD)])
        self.assertFalse(self.deployer.journal.exists())

    def test_missing_personal_database_stops_before_touching_the_service(self):
        self.personal.unlink()
        with self.assertRaisesRegex(DeployError, "Personal database"):
            self.deployer.run()
        self.assertEqual(self.deployer.installed(), OLD)
        self.assertEqual(self.host.events, [])

    def test_process_interruption_is_recovered_before_next_probe(self):
        def crash(sha):
            if sha == NEW:
                raise SystemExit("Injected process termination")

        self.host.after_start = crash
        with self.assertRaises(SystemExit):
            self.deployer.run()
        self.assertTrue(self.deployer.journal.exists())
        self.assertEqual(self.deployer.installed(), NEW)
        self.host.after_start = None
        self.upload(OLD)
        self.assertEqual(self.deployer.run(), "unchanged")
        self.assertEqual(self.deployer.installed(), OLD)
        self.assertFalse(self.deployer.journal.exists())

    def test_failed_stop_during_rollback_does_not_repoint_running_new_process(self):
        self.host.failed_ready.add(NEW)
        self.host.fail_stop_number = 2
        with self.assertRaisesRegex(DeployError, "stop failure"):
            self.deployer.run()
        self.assertEqual(self.deployer.installed(), NEW)
        self.assertTrue(self.deployer.journal.exists())
        self.assertNotIn(("start", OLD), self.host.events)

    def test_initial_failed_release_leaves_no_current_and_no_data_restore(self):
        (self.root / "current").unlink()
        self.host.failed_ready.add(NEW)
        with self.assertRaises(DeployError):
            self.deployer.run()
        self.assertIsNone(self.deployer.installed())
        self.assertFalse(self.deployer.journal.exists())
        self.assertEqual(database_value(self.personal), "before")

    def test_lock_excludes_second_deployer(self):
        with (self.root / "deploy.lock").open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.deployer.run(), "busy")
        self.assertTrue((self.root / "incoming" / f"mai-journal-{NEW}.tar.gz").exists())

    def test_invalid_journal_or_foreign_current_fails_closed(self):
        atomic_json(self.deployer.journal, {"previous": "bad", "target": NEW})
        with self.assertRaises(DeployError):
            self.deployer.run()
        self.deployer.journal.unlink()
        (self.root / "current").unlink()
        (self.root / "current").symlink_to(self.root / "shared")
        with self.assertRaises(DeployError):
            self.deployer.run()
        self.assertEqual(self.host.events, [])

    def test_unit_keeps_app_readonly_and_single_worker_with_exact_sudo(self):
        deploy = Path(__file__).resolve().parents[1] / "deploy"
        unit = (deploy / "mai-journal.service").read_text()
        self.assertIn("--workers 1", unit)
        self.assertIn("--host 127.0.0.1", unit)
        self.assertIn("TimeoutStopSec=60", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("ProtectSystem=strict", unit)
        writes = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
        self.assertEqual(
            writes,
            ["ReadWritePaths=/srv/mai-journal/shared/runtime /srv/mai-journal/shared/catalog"],
        )
        sudoers = (deploy / "mai-journal.sudoers").read_text()
        self.assertNotIn("*", sudoers)
        self.assertNotIn("ALL=(root) NOPASSWD: ALL", sudoers)


if __name__ == "__main__":
    unittest.main()
