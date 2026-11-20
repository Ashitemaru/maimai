"""Release packaging, CI upload, archive safety, and the deploy/rollback state machine."""

import contextlib
import io
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.ci_receive import receive
from deploy.deploy_release import Deployer, DeployError, extract_archive, switch_current
from scripts.package_release import package
from tests.dependency_fixtures import dependency_files, write_bundle, write_files

OLD, NEW, OTHER = "a" * 40, "b" * 40, "c" * 40
RELEASE_CODE = (
    "backend/app.py",
    "backend/store.py",
    "scripts/catalog_pipeline.py",
    "frontend/dist/index.html",
)


def archive_bytes(sha=NEW, additions=()):
    contents = {"RELEASE_SHA": sha + "\n", "requirements.lock": "example==1.0.0\n"}
    contents |= {name: "# Synthetic fixture\n" for name in RELEASE_CODE} | dependency_files()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, value in contents.items():
            member = tarfile.TarInfo(name)
            member.size = len(value.encode())
            tar.addfile(member, io.BytesIO(value.encode()))
        for item in additions:
            tar.addfile(item, io.BytesIO(b"x" * item.size) if item.isfile() else None)
    return buffer.getvalue()


class FakeHost:
    def __init__(self, root):
        self.root, self.events, self.failed_ready = root, [], set()
        self.fail_install, self.after_start = False, None

    def install(self, release):
        self.events.append(("install", release.name))
        if self.fail_install:
            raise DeployError("Injected install failure")
        (release / "venv/bin").mkdir(parents=True)
        (release / "venv/bin/python").write_text("fixture")

    def service(self, action):
        current = self.root / "current"
        sha = current.resolve().name if current.is_symlink() else None
        self.events.append((action, sha))
        if action == "start" and self.after_start:
            self.after_start(sha)

    def ready(self, sha):
        self.events.append(("ready", sha))
        return sha not in self.failed_ready


def write_value(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS fixture (value TEXT)")
        conn.execute("DELETE FROM fixture")
        conn.execute("INSERT INTO fixture VALUES (?)", (value,))


def read_value(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT value FROM fixture").fetchone()[0]


class ArchiveTests(unittest.TestCase):
    def extract(self, data):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        archive, target = Path(temporary.name) / "release.tar.gz", Path(temporary.name) / "code"
        archive.write_bytes(data)
        target.mkdir()
        extract_archive(archive, target, NEW)
        return target

    def test_valid_archive_extracts_with_matching_sha(self):
        self.assertEqual((self.extract(archive_bytes()) / "RELEASE_SHA").read_text().strip(), NEW)

    def test_traversal_private_links_and_wrong_sha_are_rejected(self):
        unsafe = [
            tarfile.TarInfo(name)
            for name in (
                "../outside",
                "/outside",
                "backend/runtime/personal.sqlite3",
                "deploy/app.env",
            )
        ]
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            link = tarfile.TarInfo("backend/link.py")
            link.type, link.linkname = kind, "../../outside"
            unsafe.append(link)
        for member in unsafe:
            with self.subTest(member=member.name, kind=member.type), self.assertRaises(DeployError):
                self.extract(archive_bytes(additions=[member]))
        with self.assertRaisesRegex(DeployError, "RELEASE_SHA"):
            self.extract(archive_bytes(OLD))


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "releases" / OLD).mkdir(parents=True)
        (self.root / "releases" / OLD / "RELEASE_SHA").write_text(OLD)
        switch_current(self.root, OLD)
        self.personal = self.root / "shared/runtime/personal.sqlite3"
        write_value(self.personal, "before")
        self.host = FakeHost(self.root)
        self.deployer = Deployer(self.root, self.host)
        self.upload()

    def upload(self, sha=NEW):
        incoming = self.root / "incoming"
        incoming.mkdir(exist_ok=True)
        (incoming / f"mai-journal-{sha}.tar.gz").write_bytes(archive_bytes(sha))

    def test_success_stops_old_then_starts_and_checks_exact_sha(self):
        self.assertEqual(self.deployer.run(), "deployed")
        self.assertEqual(
            self.host.events, [("install", NEW), ("stop", OLD), ("start", NEW), ("ready", NEW)]
        )
        self.assertEqual(self.deployer.installed(), NEW)
        self.assertEqual(self.deployer.run(), "pending")  # Upload was consumed.

    def test_failed_readiness_rolls_back_code_but_keeps_new_personal_data(self):
        self.host.failed_ready.add(NEW)
        self.host.after_start = lambda sha: sha == NEW and write_value(self.personal, "new app")
        with self.assertRaisesRegex(DeployError, "did not become ready"):
            self.deployer.run()
        self.assertEqual(self.deployer.installed(), OLD)
        self.assertEqual(self.host.events[-3:], [("stop", NEW), ("start", OLD), ("ready", OLD)])
        self.assertEqual(read_value(self.personal), "new app")
        self.assertFalse(self.deployer.journal.exists())

    def test_failed_install_never_stops_the_running_app(self):
        self.host.fail_install = True
        with self.assertRaises(DeployError):
            self.deployer.run()
        self.assertEqual(self.deployer.installed(), OLD)
        self.assertNotIn("stop", [event for event, _ in self.host.events])

    def test_interrupted_deploy_is_rolled_back_on_next_run(self):
        def crash(sha):
            if sha == NEW:
                raise SystemExit("Injected process termination")

        self.host.after_start = crash
        with self.assertRaises(SystemExit):
            self.deployer.run()
        self.assertTrue(self.deployer.journal.exists())
        self.host.after_start = None
        self.upload(OLD)
        self.assertEqual(self.deployer.run(), "unchanged")
        self.assertEqual(self.deployer.installed(), OLD)
        self.assertFalse(self.deployer.journal.exists())


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_upload_replaces_previous_pending_archive(self):
        incoming = self.root / "incoming"
        incoming.mkdir()
        (incoming / f"mai-journal-{OTHER}.tar.gz").write_bytes(b"older")
        self.assertEqual(receive(io.BytesIO(NEW.encode() + b"\narchive"), self.root), (NEW, 7))
        target = incoming / f"mai-journal-{NEW}.tar.gz"
        self.assertEqual((list(incoming.iterdir()), target.read_bytes()), ([target], b"archive"))

    def test_invalid_sha_header_or_oversized_body_leaves_no_file(self):
        for header in (b"not-a-sha\n", b"A" * 40 + b"\n"):
            with self.subTest(header=header), self.assertRaises(DeployError):
                receive(io.BytesIO(header + b"x"), self.root)
        with patch("deploy.ci_receive.MAX_ARCHIVE", 4), self.assertRaises(DeployError):
            receive(io.BytesIO(NEW.encode() + b"\n12345"), self.root)
        self.assertEqual(list((self.root / "incoming").iterdir()), [])


class PackageTests(unittest.TestCase):
    packaged = (
        "backend/app.py",
        "backend/store.py",
        "scripts/catalog_pipeline.py",
        "deploy/deploy_release.py",
        "frontend/dist/index.html",
    )
    private = (
        "backend/runtime/secrets.json",
        "deploy/runtime.json",
        "data/catalog/personal.sqlite3",
    )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        write_files(self.root, {name: "fixture" for name in self.packaged + self.private})
        write_bundle(self.root, "release-dependencies")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)

    def test_package_contains_only_release_files_and_is_accepted_by_server(self):
        archive = package(self.root, self.root / "release", NEW)
        with tarfile.open(archive) as tar:
            expected = {*self.packaged, "requirements.lock", "RELEASE_SHA", *dependency_files()}
            self.assertEqual(set(tar.getnames()), expected)
        (self.root / "extracted").mkdir()
        extract_archive(archive, self.root / "extracted", NEW)

    def test_wheel_with_wrong_hash_is_rejected(self):
        (self.root / "release-dependencies/example-1.0.0-py3-none-any.whl").write_text("tampered")
        with self.assertRaisesRegex(DeployError, "checksum mismatch"):
            package(self.root, self.root / "release", NEW)


if __name__ == "__main__":
    unittest.main()
