import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deploy.deploy_release import DeployError, Host, dependency_bundle, requirement_pins
from scripts.build_release_dependencies import create_manifest
from tests.dependency_fixtures import write_bundle


class DependencyBundleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        write_bundle(self.root)
        self.manifest = self.root / "dependencies/manifest.json"
        self.wheel = self.root / "dependencies/example-1.0.0-py3-none-any.whl"

    def compatible_host(self):
        for target, value in (
            ("platform.system", "Linux"),
            ("platform.machine", "x86_64"),
            ("platform.libc_ver", ("glibc", "2.43")),
        ):
            mock = patch("deploy.deploy_release." + target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        version = patch(
            "deploy.deploy_release.sys.version_info", SimpleNamespace(major=3, minor=14)
        )
        version.start()
        self.addCleanup(version.stop)

    def test_manifest_generation_matches_validation(self):
        original = json.loads(self.manifest.read_text())
        self.manifest.unlink()
        created = create_manifest(
            self.root / "requirements.lock", self.wheel.parent, original["target"]
        )
        self.manifest.write_text(json.dumps(created))
        locks = dependency_bundle(self.root)
        self.assertEqual(set(locks), {"example==1.0.0", "pip==26.2.1"})
        self.assertIn(" --hash=sha256:", locks["example==1.0.0"])

    def test_install_uses_only_bundled_hashed_wheels(self):
        self.compatible_host()
        host = Host()
        with patch.object(host, "run") as run:
            host.install(self.root)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 4)
        for command in commands[1:3]:
            self.assertIn("--no-index", command)
            self.assertIn("--require-hashes", command)
            self.assertIn("--no-deps", command)
            self.assertNotIn("--index-url", command)
            self.assertFalse(any("https://" in word for word in command))
            self.assertEqual(command[command.index("--find-links") + 1], str(self.wheel.parent))
        self.assertEqual(commands[-1][-1], "check")
        self.assertIn("--hash=sha256:", (self.root / "install-requirements.txt").read_text())

    def test_old_release_has_no_network_fallback(self):
        self.manifest.unlink()
        host = Host()
        with patch.object(host, "run") as run, self.assertRaisesRegex(DeployError, "rebuild"):
            host.install(self.root)
        run.assert_not_called()

    def test_reject_corrupted_wheels_before_creating_environment(self):
        self.compatible_host()
        self.wheel.write_text("changed")
        host = Host()
        with patch.object(host, "run") as run, self.assertRaisesRegex(DeployError, "checksum"):
            host.install(self.root)
        run.assert_not_called()

    def test_reject_changed_requirements_or_unlisted_wheels(self):
        (self.root / "requirements.lock").write_text("example==2.0.0\n")
        with self.assertRaisesRegex(DeployError, "does not match requirements"):
            dependency_bundle(self.root)
        (self.root / "requirements.lock").write_text("example==1.0.0\n")
        (self.wheel.parent / "unknown-1.0-py3-none-any.whl").write_text("extra")
        with self.assertRaisesRegex(DeployError, "unexpected files"):
            dependency_bundle(self.root)

    def test_reject_unsafe_manifest_paths_or_duplicate_packages(self):
        original = self.manifest.read_text()
        for update in (
            {"filename": "../../example-1.0.0-py3-none-any.whl"},
            {"filename": "example-2.0.0-py3-none-any.whl"},
            {"requirement": "pip==26.2.1"},
            {"requirement": "example @ https://example.test/wheel"},
        ):
            with self.subTest(update=update):
                data = json.loads(original)
                data["wheels"][0].update(update)
                self.manifest.write_text(json.dumps(data))
                with self.assertRaises(DeployError):
                    dependency_bundle(self.root)

    def test_reject_symlinks(self):
        replacement = self.root / "outside.whl"
        self.wheel.replace(replacement)
        self.wheel.symlink_to(replacement)
        with self.assertRaisesRegex(DeployError, "Missing or unsafe"):
            dependency_bundle(self.root)

    def test_reject_incompatible_host_before_install(self):
        self.compatible_host()
        for target, value in (
            ("platform.machine", "aarch64"),
            ("platform.system", "Darwin"),
            ("platform.libc_ver", ("glibc", "2.28")),
        ):
            with (
                self.subTest(target=target),
                patch("deploy.deploy_release." + target, return_value=value),
            ):
                with self.assertRaisesRegex(DeployError, "requires CPython"):
                    dependency_bundle(self.root, check_host=True)

    def test_requirement_names_are_normalized_and_unique(self):
        path = self.root / "requirements.lock"
        path.write_text("Some_Package==1.2.3\n")
        self.assertEqual(requirement_pins(path), {"some-package==1.2.3"})
        path.write_text("Some_Package==1.2.3\nsome-package==2.0\n")
        with self.assertRaisesRegex(DeployError, "duplicate packages"):
            requirement_pins(path)
