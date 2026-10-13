import contextlib
import fcntl
import hashlib
import io
import sqlite3
import tarfile
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from deploy.deploy_release import (
    DOWNLOAD_HOSTS,
    Deployer,
    DeployError,
    GitHub,
    OfficialRedirects,
    atomic_json,
    checksum_for,
    extract_archive,
    https_url,
    switch_current,
)
from tests.dependency_fixtures import dependency_files

OLD = "a" * 40
NEW = "b" * 40
OTHER = "c" * 40
REPO = "fixture/mai-journal"
ASSET_API = f"https://api.github.com/repos/{REPO}/releases/assets/42"
CHECKSUM_API = f"https://api.github.com/repos/{REPO}/releases/assets/43"


class ArchiveResponse:
    def __init__(self, chunks, *, status=200, headers=None, url=None):
        self.chunks = iter(chunks)
        self.status = status
        self.headers = headers or {}
        self.url = url or "https://release-assets.githubusercontent.com/fixture"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def geturl(self):
        return self.url

    def read1(self, _):
        chunk = next(self.chunks, b"")
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    read = read1


def archive_bytes(sha=NEW, additions=(), replacements=None):
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
        assets = {name: ASSET_API, "SHA256SUMS": CHECKSUM_API}
        with (
            patch.object(github, "read", return_value=manifest),
            patch.object(github.opener, "open", return_value=ArchiveResponse([content])),
        ):
            github.download(assets, NEW, self.root / "download.tar.gz")
        with (
            patch.object(github, "read", return_value=manifest),
            patch.object(github.opener, "open", return_value=ArchiveResponse([b"corrupted"])),
        ):
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


class ArchiveDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "archive.tar.gz"
        self.github = GitHub(REPO)
        self.content = b"first-second-last"
        self.name = f"mai-journal-{NEW}.tar.gz"
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.manifest = f"{self.digest} {self.name}".encode()
        self.assets = {self.name: ASSET_API, "SHA256SUMS": CHECKSUM_API}
        self.partial = self.destination.with_name(f"{self.destination.name}.{self.digest}.part")
        patcher = patch.object(self.github, "read", return_value=self.manifest)
        patcher.start()
        self.addCleanup(patcher.stop)

    def transfer(self, replies):
        with patch.object(self.github.opener, "open", side_effect=replies) as opened:
            self.github.download(self.assets, NEW, self.destination)
        return opened

    def remainder(self, offset):
        return ArchiveResponse(
            [self.content[offset:]],
            status=206,
            headers={
                "Content-Range": f"bytes {offset}-{len(self.content) - 1}/{len(self.content)}",
                "Content-Length": str(len(self.content) - offset),
            },
        )

    def test_interrupted_attempt_resumes_only_bytes_written_to_disk(self):
        opened = self.transfer(
            [
                ArchiveResponse([self.content[:6], TimeoutError("fixture timeout")]),
                self.remainder(6),
            ]
        )
        self.assertEqual(self.destination.read_bytes(), self.content)
        self.assertEqual(opened.call_args_list[1].args[0].get_header("Range"), "bytes=6-")
        self.assertIsNone(opened.call_args_list[0].args[0].get_header("Range"))
        for call in opened.call_args_list:
            request = call.args[0]
            self.assertEqual(request.full_url, ASSET_API)
            self.assertEqual(request.get_header("Accept"), "application/octet-stream")
        self.assertFalse(self.partial.exists())

    def test_checksum_and_archive_api_requests_accept_direct_binary_responses(self):
        github = GitHub(REPO)
        with patch.object(
            github.opener,
            "open",
            side_effect=[
                ArchiveResponse([self.manifest], url=CHECKSUM_API),
                ArchiveResponse([self.content], url=ASSET_API),
            ],
        ) as opened:
            github.download(self.assets, NEW, self.destination)
        requests = [call.args[0] for call in opened.call_args_list]
        self.assertEqual([request.full_url for request in requests], [CHECKSUM_API, ASSET_API])
        for request in requests:
            self.assertEqual(request.get_header("Accept"), "application/octet-stream")
        self.assertEqual(self.destination.read_bytes(), self.content)

    def test_checksum_and_archive_api_responses_can_finish_on_official_cdn(self):
        github = GitHub(REPO)
        with patch.object(
            github.opener,
            "open",
            side_effect=[ArchiveResponse([self.manifest]), ArchiveResponse([self.content])],
        ):
            github.download(self.assets, NEW, self.destination)
        self.assertEqual(self.destination.read_bytes(), self.content)

    def test_api_asset_redirect_preserves_binary_and_range_headers(self):
        request = urllib.request.Request(
            ASSET_API, headers={"Accept": "application/octet-stream", "Range": "bytes=6-"}
        )
        redirect = OfficialRedirects().redirect_request(
            request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/fixture"
        )
        self.assertEqual(redirect.get_header("Accept"), "application/octet-stream")
        self.assertEqual(redirect.get_header("Range"), "bytes=6-")
        for url in (
            "https://api.github.com/repos/other/repo/releases/assets/42",
            "https://example.test/file",
            "http://release-assets.githubusercontent.com/file",
        ):
            with self.subTest(url=url), self.assertRaises(DeployError):
                OfficialRedirects().redirect_request(request, None, 302, "Found", {}, url)

    def test_download_rejects_arbitrary_api_paths_or_foreign_repository_before_request(self):
        invalid = [
            "https://api.github.com/repos/other/repo/releases/assets/42",
            f"https://api.github.com/repos/{REPO}/contents/file",
            ASSET_API + "?redirect=1",
            ASSET_API + "/extra",
            ASSET_API.replace("/42", "/0"),
            ASSET_API.replace("/42", "/-1"),
        ]
        for url in invalid:
            for name in (self.name, "SHA256SUMS"):
                with self.subTest(url=url, asset=name):
                    github = GitHub(REPO)
                    with patch.object(github.opener, "open") as opened:
                        with self.assertRaises(DeployError):
                            github.download({**self.assets, name: url}, NEW, self.destination)
                    opened.assert_not_called()

    def test_partial_survives_failed_timer_and_fresh_downloader_resumes(self):
        with patch("deploy.deploy_release.DOWNLOAD_ATTEMPTS", 1):
            with self.assertRaisesRegex(DeployError, "retained 6 bytes"):
                self.transfer([ArchiveResponse([self.content[:6], TimeoutError()])])
        self.assertEqual(self.partial.read_bytes(), self.content[:6])
        self.assertFalse(self.destination.exists())
        fresh = GitHub(REPO)
        with (
            patch.object(fresh, "read", return_value=self.manifest),
            patch.object(fresh.opener, "open", return_value=self.remainder(6)) as opened,
        ):
            fresh.download(self.assets, NEW, self.destination)
        self.assertEqual(opened.call_args.args[0].get_header("Range"), "bytes=6-")
        self.assertEqual(self.destination.read_bytes(), self.content)

    def test_server_ignoring_range_restarts_without_appending(self):
        self.partial.write_bytes(self.content[:6])
        opened = self.transfer(
            [ArchiveResponse([self.content], headers={"Content-Length": str(len(self.content))})]
        )
        self.assertEqual(opened.call_args.args[0].get_header("Range"), "bytes=6-")
        self.assertEqual(self.destination.read_bytes(), self.content)

    def test_short_body_resumes_instead_of_hashing_incomplete_file(self):
        opened = self.transfer(
            [
                ArchiveResponse(
                    [self.content[:6]], headers={"Content-Length": str(len(self.content))}
                ),
                self.remainder(6),
            ]
        )
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(self.destination.read_bytes(), self.content)

    def test_complete_partial_from_previous_run_is_verified_without_redownload(self):
        self.partial.write_bytes(self.content)
        opened = self.transfer([])
        opened.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), self.content)

    def test_invalid_ranges_are_rejected_and_partial_is_discarded(self):
        headers = [
            {},
            {"Content-Range": "bytes 0-16/17"},
            {"Content-Range": "bytes 6-15/17"},
            {"Content-Range": "bytes 6-16/*"},
            {"Content-Range": "bytes 6-16/17", "Content-Length": "12"},
            {"Content-Range": "bytes 6-16/17", "Content-Encoding": "gzip"},
            {"Content-Range": "bytes 6-999999999/1000000000"},
        ]
        for row in headers:
            with self.subTest(headers=row):
                self.partial.write_bytes(self.content[:6])
                with self.assertRaises(DeployError):
                    self.transfer([ArchiveResponse([self.content[6:]], status=206, headers=row)])
                self.assertFalse(self.partial.exists())
                self.assertFalse(self.destination.exists())

    def test_checksum_failure_discards_completed_bad_partial(self):
        with self.assertRaisesRegex(DeployError, "checksum mismatch"):
            self.transfer([ArchiveResponse([b"wrong payload"])])
        self.assertFalse(self.partial.exists())
        self.assertFalse(self.destination.exists())

    def test_declared_or_actual_oversize_fails_before_activation(self):
        with patch("deploy.deploy_release.MAX_ARCHIVE", 5):
            for headers in ({"Content-Length": "6"}, {}):
                with self.subTest(headers=headers), self.assertRaises(DeployError):
                    self.transfer([ArchiveResponse([b"123456"], headers=headers)])
                self.assertFalse(self.partial.exists())

    def test_cached_symlinks_are_never_read_or_written(self):
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        for path in (self.destination, self.partial):
            with self.subTest(path=path):
                path.symlink_to(outside)
                with self.assertRaisesRegex(DeployError, "regular files"):
                    self.transfer([])
                self.assertEqual(outside.read_bytes(), b"untouched")
                path.unlink()

    def test_response_destination_still_requires_official_https(self):
        with self.assertRaisesRegex(DeployError, "unofficial"):
            self.transfer([ArchiveResponse([self.content], url="https://example.test/archive")])
        self.assertFalse(self.destination.exists())

    def test_retry_budget_preserves_bytes_without_an_unbounded_loop(self):
        with patch("deploy.deploy_release.DOWNLOAD_ATTEMPTS", 2):
            with self.assertRaisesRegex(DeployError, "retained 6 bytes"):
                self.transfer([ArchiveResponse([self.content[:6], TimeoutError()]), TimeoutError()])
        self.assertEqual(self.partial.read_bytes(), self.content[:6])

    def test_deadline_keeps_progress_even_without_content_length(self):
        with patch("deploy.deploy_release.time.monotonic", side_effect=[0, 1, 2, 3, 500, 500]):
            with self.assertRaisesRegex(DeployError, "retained 6 bytes"):
                self.transfer([ArchiveResponse([self.content[:6], self.content[6:]])])
        self.assertEqual(self.partial.read_bytes(), self.content[:6])


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
                    "id": asset_id,
                    "url": f"https://api.github.com/repos/{REPO}/releases/assets/{asset_id}",
                    "state": "uploaded",
                    "browser_download_url": f"https://github.com/{REPO}/releases/download/app-{NEW}/{name}",
                }
                for asset_id, name in enumerate(
                    (f"mai-journal-{NEW}.tar.gz", "SHA256SUMS"), start=42
                )
            ],
        }

    def test_only_successful_main_push_in_same_repository_is_accepted(self):
        with patch.object(
            self.github,
            "api",
            side_effect=[{"workflow_runs": [self.run]}, self.release, {"sha": NEW}],
        ):
            self.assertEqual(
                self.github.release(NEW),
                {f"mai-journal-{NEW}.tar.gz": ASSET_API, "SHA256SUMS": CHECKSUM_API},
            )
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

    def test_asset_api_identity_must_match_positive_integer_id_and_exact_repository(self):
        original = dict(self.release["assets"][0])
        for changes in (
            {"id": True},
            {"id": 0},
            {"id": -1},
            {"id": "42"},
            {"id": 43},
            {"url": ASSET_API + "?extra=1"},
            {"url": "https://api.github.com/repos/other/repo/releases/assets/42"},
            {"url": f"https://api.github.com/repos/{REPO}/contents/file"},
        ):
            with self.subTest(changes=changes):
                self.release["assets"][0] = {**original, **changes}
                with patch.object(
                    self.github,
                    "api",
                    side_effect=[{"workflow_runs": [self.run]}, self.release, {"sha": NEW}],
                ):
                    with self.assertRaisesRegex(DeployError, "asset API URL"):
                        self.github.release(NEW)

    def test_metadata_requests_keep_json_accept(self):
        url = self.github.base + "/commits/main"
        with patch.object(
            self.github.opener, "open", return_value=ArchiveResponse([b"{}"], url=url)
        ) as opened:
            self.assertEqual(self.github.read(url, 100), b"{}")
        self.assertEqual(
            opened.call_args.args[0].get_header("Accept"), "application/vnd.github+json"
        )


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

    def test_partial_download_failure_keeps_old_service_and_survives_timer(self):
        github = GitHub(REPO)
        content = archive_bytes()
        digest = hashlib.sha256(content).hexdigest()
        name = f"mai-journal-{NEW}.tar.gz"
        destination = self.root / "downloads" / name
        partial = destination.with_name(f"{name}.{digest}.part")
        manifest = f"{digest} {name}".encode()
        assets = {name: ASSET_API, "SHA256SUMS": CHECKSUM_API}
        with (
            patch.object(github, "read", return_value=manifest),
            patch.object(github, "main_sha", return_value=NEW),
            patch.object(github, "release", return_value=assets),
            patch("deploy.deploy_release.DOWNLOAD_ATTEMPTS", 1),
            patch.object(
                github.opener, "open", return_value=ArchiveResponse([content[:20], TimeoutError()])
            ),
        ):
            with self.assertRaisesRegex(DeployError, "retained 20 bytes"):
                Deployer(self.root, github, self.host).run()
        self.assertEqual(partial.read_bytes(), content[:20])
        self.assertEqual(self.host.events, [])
        self.assertEqual(self.deployer.installed(), OLD)
        self.assertFalse(self.deployer.journal.exists())
        with (
            patch.object(github, "read", return_value=manifest),
            patch.object(github, "main_sha", return_value=NEW),
            patch.object(github, "release", return_value=assets),
            patch.object(
                github.opener,
                "open",
                return_value=ArchiveResponse(
                    [content[20:]],
                    status=206,
                    headers={"Content-Range": f"bytes 20-{len(content) - 1}/{len(content)}"},
                ),
            ) as opened,
        ):
            self.assertEqual(Deployer(self.root, github, self.host).run(), "deployed")
        self.assertEqual(opened.call_args.args[0].get_header("Range"), "bytes=20-")
        self.assertFalse(partial.exists())
        self.assertFalse(destination.exists())

    def test_download_cache_symlink_is_rejected_before_network_or_service_changes(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "downloads").symlink_to(outside)
        with self.assertRaisesRegex(DeployError, "real directory"):
            self.deployer.run()
        self.assertEqual(self.host.events, [])
        self.assertNotIn(("download", NEW), self.github.requests)

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
        self.assertIn("TimeoutStopSec=210", unit)
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
