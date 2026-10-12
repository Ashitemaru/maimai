import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from deploy.deploy_release import CORE_SCRIPTS as DEPLOY_SCRIPTS
from deploy.deploy_release import extract_archive
from scripts.package_release import CORE_SCRIPTS, main, package, publish_release, run_gh

SHA = "a" * 40


class ReleaseDiagnosticTest(unittest.TestCase):
    def test_cli_failure_reports_phase_exit_code_and_cause(self):
        result = subprocess.CompletedProcess(
            [],
            1,
            stdout="private stdout",
            stderr="HTTP 403: Resource not accessible by integration",
        )
        with patch("scripts.package_release.subprocess.run", return_value=result):
            with self.assertRaisesRegex(
                RuntimeError,
                r"gh release create failed \(exit 1\): HTTP 403: Resource not accessible by integration",
            ) as raised:
                run_gh("release", "create", "private-argument")
        self.assertNotIn("private-argument", str(raised.exception))
        self.assertNotIn("private stdout", str(raised.exception))

    def test_missing_release_still_allows_creation(self):
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="gh: Not Found (HTTP 404)")
        with patch("scripts.package_release.subprocess.run", return_value=result):
            self.assertIsNone(run_gh("api", "fixture", allow_missing=True))
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                run_gh("api", "fixture")

    def test_cli_diagnostic_redacts_credentials_and_full_urls(self):
        diagnostic = (
            "HTTP 403: denied\n"
            "opaque-fixture-token ghp_fixturetoken github_pat_fixturetoken\n"
            "Authorization: Bearer secret-header\n"
            "Proxy-Authorization=Basic secret-proxy\n"
            "download https://user:password@example.com/asset?sig=private-signature failed\n"
            "\x1b[31mretry later\x1b[0m"
        )
        result = subprocess.CompletedProcess([], 1, stdout="", stderr=diagnostic)
        with (
            patch.dict("os.environ", {"GH_TOKEN": "opaque-fixture-token"}),
            patch("scripts.package_release.subprocess.run", return_value=result),
            self.assertRaises(RuntimeError) as raised,
        ):
            run_gh("release", "download", "fixture")
        message = str(raised.exception)
        self.assertIn("HTTP 403: denied", message)
        self.assertIn("retry later", message)
        for secret in (
            "opaque-fixture-token",
            "ghp_fixturetoken",
            "github_pat_fixturetoken",
            "secret-header",
            "secret-proxy",
            "example.com",
            "private-signature",
            "\x1b",
        ):
            self.assertNotIn(secret, message)

    def test_main_emits_one_escaped_annotation_for_publish_failure(self):
        output = io.StringIO()
        with (
            patch("scripts.package_release.subprocess.check_output", return_value=SHA.encode()),
            patch("scripts.package_release.package", return_value=Path("release/fixture.tar.gz")),
            patch(
                "scripts.package_release.publish_release",
                side_effect=RuntimeError("HTTP 403: denied 50%\r\n::warning::injected"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["--publish", "fixture/repository"]), 1)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            lines[1],
            "::error title=Release failed::Publish or verify release: RuntimeError: "
            "HTTP 403: denied 50%25%0D%0A::warning::injected",
        )

    def test_main_reports_packaging_failure_and_does_not_publish(self):
        output = io.StringIO()
        with (
            patch("scripts.package_release.subprocess.check_output", return_value=SHA.encode()),
            patch(
                "scripts.package_release.package",
                side_effect=ValueError("Required release file missing: frontend/dist/index.html"),
            ),
            patch("scripts.package_release.publish_release") as publish,
            redirect_stdout(output),
        ):
            self.assertEqual(main(["--publish", "fixture/repository"]), 1)
        publish.assert_not_called()
        self.assertIn(
            "::error title=Release failed::Package release: ValueError: Required release file missing",
            output.getvalue(),
        )


class FakeReleaseService:
    def __init__(self, destination):
        self.destination = destination
        self.metadata = None
        self.sha = SHA
        self.assets = {}
        self.created = 0

    def __call__(self, *args, **kwargs):
        if args[0] == "api":
            if "/commits/" in args[1]:
                return json.dumps({"sha": self.sha})
            return json.dumps(self.metadata) if self.metadata is not None else None
        if args[:2] == ("release", "create"):
            self.created += 1
            self.assets = {path.name: path.read_bytes() for path in self.destination.iterdir()}
            self.metadata = {
                "tag_name": f"app-{SHA}",
                "draft": False,
                "prerelease": False,
                "assets": [{"name": name, "state": "uploaded"} for name in self.assets],
            }
            return ""
        if args[:2] == ("release", "download"):
            folder = Path(args[args.index("--dir") + 1])
            for name, payload in self.assets.items():
                (folder / name).write_bytes(payload)
            return ""
        raise AssertionError(f"Unexpected command: {args}")


class PackageReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in (
            "backend/app.py",
            "backend/store.py",
            "backend/test_app.py",
            "backend/testing.py",
            "backend/runtime/private.py",
            "scripts/catalog_pipeline.py",
            "scripts/package_release.py",
            "scripts/nested/catalog_pipeline.py",
            "deploy/deploy_release.py",
            "deploy/wahlap-runtime.lock.json",
            "deploy/runtime.json",
            "deploy/core.dat",
            "requirements.lock",
            "frontend/dist/index.html",
            "backend/runtime/secrets.json",
            "data/catalog/personal.sqlite3",
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        (self.root / "requirements.lock").write_text("example==1.0.0\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)

    def test_only_code_and_built_frontend_are_packaged(self):
        archive = package(self.root, self.root / "release", "a" * 40)
        with tarfile.open(archive) as tar:
            self.assertEqual(
                set(tar.getnames()),
                {
                    "backend/app.py",
                    "backend/store.py",
                    "scripts/catalog_pipeline.py",
                    "deploy/deploy_release.py",
                    "deploy/wahlap-runtime.lock.json",
                    "requirements.lock",
                    "frontend/dist/index.html",
                    "RELEASE_SHA",
                },
            )
        first = archive.read_bytes()
        self.assertEqual(first, package(self.root, self.root / "release", "a" * 40).read_bytes())
        self.assertEqual(
            (archive.parent / "SHA256SUMS").read_text().split()[0],
            hashlib.sha256(first).hexdigest(),
        )

    def test_symlink_input_is_rejected(self):
        path = self.root / "backend/app.py"
        path.unlink()
        path.symlink_to(self.root / "backend/runtime/secrets.json")
        with self.assertRaises(ValueError):
            package(self.root, self.root / "release", "a" * 40)

    def test_archive_is_accepted_by_real_server_extractor(self):
        archive = package(self.root, self.root / "release", SHA)
        destination = self.root / "extracted"
        destination.mkdir()
        extract_archive(archive, destination, SHA)
        self.assertEqual((destination / "RELEASE_SHA").read_text().strip(), SHA)
        self.assertEqual(CORE_SCRIPTS, DEPLOY_SCRIPTS)

    def test_missing_runtime_module_prevents_release(self):
        (self.root / "backend/store.py").unlink()
        with self.assertRaises(ValueError):
            package(self.root, self.root / "release", SHA)

    def create_release(self):
        destination = self.root / "release"
        package(self.root, destination, SHA)
        service = FakeReleaseService(destination)
        publish_release(destination, SHA, "fixture/mai-journal", gh=service)
        return destination, service

    def test_identical_rerun_does_not_create_or_overwrite_release(self):
        destination, service = self.create_release()
        first_assets = dict(service.assets)
        publish_release(destination, SHA, "fixture/mai-journal", gh=service)
        self.assertEqual(service.created, 1)
        self.assertEqual(service.assets, first_assets)

    def test_different_archive_is_rejected_without_replacing_release(self):
        destination, service = self.create_release()
        service.assets[f"mai-journal-{SHA}.tar.gz"] = b"existing different release"
        first_assets = dict(service.assets)
        with self.assertRaisesRegex(RuntimeError, "asset differs"):
            publish_release(destination, SHA, "fixture/mai-journal", gh=service)
        self.assertEqual(service.created, 1)
        self.assertEqual(service.assets, first_assets)

    def test_changed_checksum_manifest_is_rejected(self):
        destination, service = self.create_release()
        service.assets["SHA256SUMS"] = b"different checksum manifest"
        with self.assertRaisesRegex(RuntimeError, "asset differs"):
            publish_release(destination, SHA, "fixture/mai-journal", gh=service)
        self.assertEqual(service.created, 1)

    def test_incomplete_existing_release_is_not_silently_repaired(self):
        destination, service = self.create_release()
        service.metadata["assets"].pop()
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            publish_release(destination, SHA, "fixture/mai-journal", gh=service)
        self.assertEqual(service.created, 1)

    def test_existing_tag_must_point_to_exact_commit(self):
        destination, service = self.create_release()
        service.sha = "b" * 40
        with self.assertRaisesRegex(RuntimeError, "another commit"):
            publish_release(destination, SHA, "fixture/mai-journal", gh=service)
        self.assertEqual(service.created, 1)
