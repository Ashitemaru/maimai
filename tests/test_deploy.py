import contextlib
import fcntl
import hashlib
import io
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.deploy_release import (
    DOWNLOAD_HOSTS,
    Deployer,
    DeployError,
    GitHub,
    atomic_json,
    checksum_for,
    extract_archive,
    https_url,
    switch_current,
)

OLD = "a" * 40
NEW = "b" * 40
OTHER = "c" * 40
REPO = "fixture/mai-journal"


def archive_bytes(sha=NEW, additions=(), replacements=None):
    contents = {
        "RELEASE_SHA": sha + "\n",
        "requirements.lock": "example==1.0.0\n",
        "backend/app.py": "# Synthetic application fixture\n",
        "backend/store.py": "# Synthetic store fixture\n",
        "scripts/catalog_pipeline.py": "# Synthetic catalog fixture\n",
        "frontend/dist/index.html": "<!doctype html><title>Fixture</title>",
    }
    contents.update(replacements or {})
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


class FakeGitHub:
    def __init__(self, sha=NEW):
        self.sha = sha
        self.approved = True
        self.requests = []
        self.archive = archive_bytes(sha)

    def main_sha(self):
        self.requests.append("main")
        return self.sha

    def release(self, sha):
        self.requests.append(("release", sha))
        return {} if self.approved else None

    def download(self, assets, sha, destination):
        self.requests.append(("download", sha))
        destination.write_bytes(self.archive)


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

    def test_valid_archive_is_code_only_and_has_matching_release(self):
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

    def test_checksum_requires_exact_filename_once(self):
        name, digest = f"mai-journal-{NEW}.tar.gz", "d" * 64
        self.assertEqual(checksum_for(f"{digest}  {name}\n".encode(), name), digest)
        for body in (
            f"{digest} another.tar.gz",
            f"{digest} {name}\n{digest} {name}",
            f"bad {name}",
        ):
            with self.assertRaises(DeployError):
                checksum_for(body.encode(), name)

    def test_download_checks_archive_digest(self):
        github = GitHub(REPO)
        name = f"mai-journal-{NEW}.tar.gz"
        content = archive_bytes()
        manifest = hashlib.sha256(content).hexdigest().encode() + b"  " + name.encode()
        assets = {name: "archive", "SHA256SUMS": "checksum"}
        with patch.object(github, "read", side_effect=[manifest, content]):
            github.download(assets, NEW, self.root / "download.tar.gz")
        with patch.object(github, "read", side_effect=[manifest, b"corrupted"]):
            with self.assertRaisesRegex(DeployError, "checksum mismatch"):
                github.download(assets, NEW, self.root / "bad.tar.gz")

    def test_official_https_hosts_only(self):
        for url in (
            "http://github.com/a",
            "https://github.com.evil.test/a",
            "https://user:secret@github.com/a",
            "https://github.com:8443/a",
            "file:///tmp/a",
        ):
            with self.subTest(url=url), self.assertRaises(DeployError):
                https_url(url, DOWNLOAD_HOSTS)
        self.assertEqual(
            https_url("https://release-assets.githubusercontent.com/a", DOWNLOAD_HOSTS),
            "https://release-assets.githubusercontent.com/a",
        )


class GitHubApprovalTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub(REPO)
        self.run = {
            "head_sha": NEW,
            "head_branch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": REPO},
            "head_repository": {"full_name": REPO},
        }
        self.release = {
            "tag_name": f"app-{NEW}",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": name,
                    "state": "uploaded",
                    "browser_download_url": f"https://github.com/{REPO}/releases/download/app-{NEW}/{name}",
                }
                for name in (f"mai-journal-{NEW}.tar.gz", "SHA256SUMS")
            ],
        }

    def test_only_successful_main_push_in_same_repository_is_accepted(self):
        with patch.object(
            self.github,
            "api",
            side_effect=[{"workflow_runs": [self.run]}, self.release, {"sha": NEW}],
        ):
            self.assertIn("SHA256SUMS", self.github.release(NEW))
        for key, value in (
            ("event", "pull_request"),
            ("head_branch", "topic"),
            ("head_sha", OTHER),
            ("status", "in_progress"),
            ("conclusion", "failure"),
            ("head_repository", {"full_name": "fork/repo"}),
        ):
            with (
                self.subTest(key=key),
                patch.object(
                    self.github, "api", return_value={"workflow_runs": [{**self.run, key: value}]}
                ),
            ):
                self.assertIsNone(self.github.release(NEW))

    def test_tag_commit_and_asset_url_are_revalidated(self):
        with patch.object(
            self.github,
            "api",
            side_effect=[{"workflow_runs": [self.run]}, self.release, {"sha": OLD}],
        ):
            with self.assertRaisesRegex(DeployError, "tag does not resolve"):
                self.github.release(NEW)
        self.release["assets"][0]["browser_download_url"] = "https://evil.test/archive"
        with patch.object(
            self.github,
            "api",
            side_effect=[{"workflow_runs": [self.run]}, self.release, {"sha": NEW}],
        ):
            with self.assertRaisesRegex(DeployError, "asset URL"):
                self.github.release(NEW)


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
        self.github = FakeGitHub()
        self.host = FakeHost(self.root)
        self.deployer = Deployer(self.root, self.github, self.host)

    def test_success_stops_before_backup_then_starts_exact_sha(self):
        make_database(self.root / "shared/runtime/auth.sqlite3", "persistent login")
        real_backup = self.deployer.backup

        def backup(previous, target):
            self.assertEqual(self.host.events[-1], ("stop", OLD))
            self.host.events.append(("backup", OLD))
            return real_backup(previous, target)

        with patch.object(self.deployer, "backup", side_effect=backup):
            self.assertEqual(self.deployer.run(), "deployed")
        self.assertEqual(
            self.host.events,
            [("install", NEW), ("stop", OLD), ("backup", OLD), ("start", NEW), ("ready", NEW)],
        )
        self.assertEqual(self.deployer.installed(), NEW)
        backups = list((self.root / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(database_value(backups[0] / "personal.sqlite3"), "before")
        self.assertEqual(database_value(backups[0] / "adjudications.sqlite3"), "permanent decision")
        self.assertEqual(database_value(backups[0] / "auth.sqlite3"), "persistent login")
        self.assertEqual(
            (self.root / "current/release.env").read_text(), f"MAIMAI_RELEASE_SHA={NEW}\n"
        )
        self.assertFalse(self.deployer.journal.exists())

    def test_same_sha_and_pending_ci_do_not_stop_or_install(self):
        self.github.sha = OLD
        self.assertEqual(self.deployer.run(), "unchanged")
        self.assertEqual(self.github.requests, ["main"])
        self.github.sha, self.github.approved = NEW, False
        self.assertEqual(self.deployer.run(), "pending")
        self.assertEqual(self.host.events, [])

    def test_failed_prepare_and_advanced_main_preserve_running_app(self):
        self.host.fail_install = True
        with self.assertRaises(DeployError):
            self.deployer.run()
        self.assertEqual(self.deployer.installed(), OLD)
        self.assertFalse((self.root / "releases" / NEW).exists())
        self.host.fail_install = False
        self.host.after_install = lambda: setattr(self.github, "sha", OTHER)
        self.assertEqual(self.deployer.run(), "superseded")
        self.assertTrue(all(event[0] == "install" for event in self.host.events))
        self.assertFalse(self.deployer.journal.exists())

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

    def test_backup_failure_restarts_previous_without_switch(self):
        self.personal.unlink()
        with self.assertRaisesRegex(DeployError, "Personal database"):
            self.deployer.run()
        self.assertEqual(self.deployer.installed(), OLD)
        self.assertNotIn(("start", NEW), self.host.events)
        self.assertIn(("start", OLD), self.host.events)

    def test_failed_sha_is_not_reactivated_every_timer_tick_and_can_be_retried(self):
        self.host.failed_ready.add(NEW)
        with self.assertRaises(DeployError):
            self.deployer.run()
        events = list(self.host.events)
        self.assertEqual(self.deployer.run(), "rejected")
        self.assertEqual(self.host.events, events)
        self.host.failed_ready.clear()
        retry = Deployer(self.root, self.github, self.host, retry_failed=True)
        self.assertEqual(retry.run(), "deployed")
        self.assertFalse(retry.failed_release.exists())

    def test_new_main_sha_is_eligible_after_an_older_release_failed(self):
        self.host.failed_ready.add(NEW)
        with self.assertRaises(DeployError):
            self.deployer.run()
        self.github.sha = OTHER
        self.github.archive = archive_bytes(OTHER)
        self.assertEqual(self.deployer.run(), "deployed")
        self.assertEqual(self.deployer.installed(), OTHER)

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
        self.github.sha = OLD
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
        self.assertEqual(self.github.requests, [])

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
        self.assertIn("TimeoutStopSec=90", unit)
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
