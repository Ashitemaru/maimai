"""Exercise backup orchestration without services, restic, production data or network."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from deploy.backup import BackupError, Backups, Host, atomic_json, lock, main

SHA = "a" * 40
NEXT_SHA = "b" * 40
SECRET = "fixture-private-password-never-print"
SNAPSHOT = "c" * 64


class FakeHost:
    def __init__(self, state="active"):
        self.active = state
        self.events = []
        self.fail_stop = False
        self.fail_ready = False
        self.stop_leaves_running = False
        self.main_pid = 0

    def state(self):
        self.events.append(("state", self.active))
        return self.active

    def service(self, action):
        self.events.append((action,))
        if action == "stop":
            self.active = "active" if self.stop_leaves_running else "inactive"
            if self.fail_stop:
                raise BackupError("Injected stop failure")
        elif action == "start":
            self.active = "active"

    def ready(self, sha):
        self.events.append(("ready", sha))
        if self.fail_ready:
            raise BackupError("Injected ready failure")

    def quiescent(self):
        self.events.append(("quiescent", self.main_pid))
        return self.main_pid == 0

    def command(self, *args, **kwargs):
        raise AssertionError("Use an explicit fake restic response in orchestration tests")


class BackupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="mai-backup-tests-")
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.root = self.base / "application"
        self.root.mkdir()
        (self.root / "shared/catalog/updates").mkdir(parents=True)
        self.release = self.root / "releases" / SHA
        self.release.mkdir(parents=True)
        (self.release / "RELEASE_SHA").write_text(SHA)
        (self.root / "current").symlink_to(Path("releases") / SHA)
        self.directory = self.base / "backups"
        self.directory.mkdir(mode=0o700)
        self.password = self.base / "password"
        self.password.write_text(SECRET)
        self.password.chmod(0o600)
        self.host = FakeHost()
        self.backup = Backups(
            self.root, self.directory, self.password, config=self.base / "config", host=self.host
        )
        self.backup.repository.mkdir()
        (self.backup.repository / "config").write_text("fixture encrypted repository")
        self.capture = Mock(side_effect=self.fake_capture)
        capture_patch = patch("deploy.backup.capture", self.capture)
        capture_patch.start()
        self.addCleanup(capture_patch.stop)

    def fake_capture(self, root, config, payload):
        self.assertEqual(root, self.root)
        self.assertEqual(self.host.active, "inactive")
        self.host.events.append(("capture",))
        payload.mkdir()
        (payload / "fixture").write_text("private staging data")
        return {"release_sha": SHA}

    def actions(self):
        return [event[0] for event in self.host.events if event[0] not in {"state", "quiescent"}]

    def fake_restic(self, *args, **kwargs):
        if args[0] == "backup":
            self.assertEqual(self.host.active, "active")
            self.assertFalse(self.backup.marker.exists())
            return json.dumps({"message_type": "summary", "snapshot_id": SNAPSHOT})
        return ""

    def test_snapshot_captures_quiesced_data_under_both_locks_then_resumes(self):
        original = self.capture.side_effect

        def capture_locked(*args):
            for path in (
                self.root / "deploy.lock",
                self.root / "shared/catalog/updates/publish.lock",
            ):
                with self.assertRaises(BackupError), lock(path):
                    pass
            return original(*args)

        self.capture.side_effect = capture_locked
        result = self.backup.snapshot()
        self.assertEqual(result, {"release_sha": SHA})
        self.assertEqual(self.actions(), ["stop", "capture", "start", "ready"])
        self.assertFalse(self.backup.marker.exists())
        self.assertEqual(self.host.events[-1], ("ready", SHA))
        for path in (self.root / "deploy.lock", self.root / "shared/catalog/updates/publish.lock"):
            self.assertTrue(path.exists())
            with lock(path):
                pass

    def test_capture_failure_still_restarts_service_and_removes_resume_marker(self):
        self.capture.side_effect = BackupError("Injected capture failure")
        with self.assertRaisesRegex(BackupError, "capture failure"):
            self.backup.snapshot()
        self.assertEqual(self.actions(), ["stop", "start", "ready"])
        self.assertFalse(self.backup.marker.exists())
        self.assertEqual(self.host.active, "active")

    def test_originally_inactive_or_failed_service_is_not_started(self):
        for state in ("inactive", "failed"):
            with self.subTest(state=state):
                self.host.active = state
                self.host.events.clear()
                self.capture.side_effect = lambda *args: {"release_sha": SHA}
                self.backup.snapshot()
                self.assertEqual(self.actions(), [])
                self.assertEqual(self.host.active, state)
                self.assertFalse(self.backup.marker.exists())

    def test_pending_deployment_prevents_any_stop_or_capture(self):
        (self.root / "deployment-pending.json").write_text("{}")
        with self.assertRaisesRegex(BackupError, "Deployment recovery"):
            self.backup.snapshot()
        self.assertEqual(self.host.events, [])
        self.capture.assert_not_called()
        self.assertFalse(self.backup.marker.exists())

    def test_transitional_service_is_left_alone(self):
        self.host.active = "activating"
        with self.assertRaisesRegex(BackupError, "changing state"):
            self.backup.snapshot()
        self.assertEqual(self.actions(), [])
        self.capture.assert_not_called()

    def test_stop_error_still_resumes_and_never_captures(self):
        self.host.fail_stop = True
        with self.assertRaisesRegex(BackupError, "stop failure"):
            self.backup.snapshot()
        self.assertEqual(self.actions(), ["stop", "start", "ready"])
        self.assertEqual(self.host.active, "active")
        self.assertFalse(self.backup.marker.exists())
        self.capture.assert_not_called()

    def test_stop_without_inactive_confirmation_never_captures(self):
        self.host.stop_leaves_running = True
        with self.assertRaisesRegex(BackupError, "still running"):
            self.backup.snapshot()
        self.capture.assert_not_called()
        self.assertEqual(self.actions(), ["stop", "start", "ready"])

    def test_inactive_state_with_remaining_main_pid_does_not_capture(self):
        self.host.main_pid = 1234
        with self.assertRaises(BackupError):
            self.backup.snapshot()
        self.assertIn(("quiescent", 1234), self.host.events)
        self.capture.assert_not_called()
        self.assertEqual(self.actions(), ["stop", "start", "ready"])
        self.assertFalse(self.backup.marker.exists())

    def test_busy_publication_still_resumes_after_stop(self):
        with lock(self.root / "shared/catalog/updates/publish.lock"):
            with self.assertRaises(BackupError):
                self.backup.snapshot()
        self.capture.assert_not_called()
        self.assertEqual(self.actions(), ["stop", "start", "ready"])
        self.assertFalse(self.backup.marker.exists())

    def test_process_kill_marker_is_recovered_against_current_release_and_is_idempotent(self):
        atomic_json(self.backup.marker, {"release_sha": NEXT_SHA})
        self.host.active = "inactive"
        restarted = Backups(self.root, self.directory, self.password, host=self.host)
        restarted.resume()
        restarted.resume()
        self.assertEqual(self.actions(), ["start", "ready"])
        self.assertEqual(self.host.events[-1], ("ready", SHA))
        self.assertFalse(restarted.marker.exists())

    def test_resume_failure_keeps_marker_for_next_attempt(self):
        atomic_json(self.backup.marker, {"release_sha": SHA})
        self.host.fail_ready = True
        with self.assertRaises(BackupError):
            self.backup.resume()
        self.assertTrue(self.backup.marker.exists())
        self.host.fail_ready = False
        self.backup.resume()
        self.assertFalse(self.backup.marker.exists())

    def test_resume_needs_no_password_or_working_repository(self):
        atomic_json(self.backup.marker, {"release_sha": SHA})
        self.password.unlink()
        (self.backup.repository / "config").unlink()
        self.host.active = "inactive"
        self.backup.resume()
        self.assertEqual(self.actions(), ["start", "ready"])
        self.assertFalse(self.backup.marker.exists())

    def test_snapshot_interruption_resumes_before_propagating(self):
        self.capture.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.backup.snapshot()
        self.assertEqual(self.actions(), ["stop", "start", "ready"])
        self.assertFalse(self.backup.marker.exists())

    def test_resume_does_not_interfere_with_pending_deployment(self):
        atomic_json(self.backup.marker, {"release_sha": SHA})
        (self.root / "deployment-pending.json").write_text("{}")
        with self.assertRaises(BackupError):
            self.backup.resume()
        self.assertTrue(self.backup.marker.exists())
        self.assertEqual(self.host.events, [])

    def test_success_checks_before_pruning_and_erases_plaintext_staging(self):
        for full_check in (False, True):
            with self.subTest(full_check=full_check):
                with patch.object(self.backup, "restic", side_effect=self.fake_restic) as restic:
                    result = self.backup.backup(full_check=full_check)
                commands = [call.args for call in restic.call_args_list]
                self.assertEqual(
                    [command[0] for command in commands], ["backup", "check", "forget"]
                )
                self.assertEqual(
                    commands[1][1], "--read-data" if full_check else "--read-data-subset=1/12"
                )
                self.assertIn("--prune", commands[2])
                self.assertEqual(result["state"], "complete")
                self.assertEqual(result["snapshot_id"], SNAPSHOT)
                self.assertEqual(json.loads(self.backup.receipt.read_text()), result)
                self.assertFalse(self.backup.staging.exists())

    def test_capture_backup_or_check_failure_never_prunes_and_keeps_last_success(self):
        for phase in ("capture", "backup", "check", "missing_receipt", "malformed_receipt"):
            with self.subTest(phase=phase):
                previous = "2026-10-12T20:00:00+00:00"
                atomic_json(self.backup.receipt, {"last_success": previous})
                self.capture.side_effect = (
                    BackupError(SECRET) if phase == "capture" else self.fake_capture
                )

                def restic_response(*args, phase=phase, **kwargs):
                    if args[0] == phase:
                        raise BackupError(SECRET)
                    if args[0] == "backup" and phase == "missing_receipt":
                        return "{}\n"
                    if args[0] == "backup" and phase == "malformed_receipt":
                        return SECRET
                    return self.fake_restic(*args, **kwargs)

                with patch.object(self.backup, "restic", side_effect=restic_response) as restic:
                    with self.assertRaises((BackupError, ValueError)):
                        self.backup.backup()
                self.assertNotIn("forget", [call.args[0] for call in restic.call_args_list])
                status = json.loads(self.backup.receipt.read_text())
                self.assertEqual(status["state"], "failed")
                self.assertEqual(status["last_success"], previous)
                self.assertNotIn(SECRET, self.backup.receipt.read_text())
                self.assertFalse(self.backup.staging.exists())
                self.assertEqual(self.host.active, "active")

    def test_missing_repository_or_held_backup_lock_never_stops_application(self):
        with lock(self.directory / "backup.lock"):
            with self.assertRaises(BackupError):
                self.backup.backup()
        (self.backup.repository / "config").unlink()
        with self.assertRaisesRegex(BackupError, "Initialize"):
            self.backup.backup()
        self.assertEqual(self.host.events, [])
        self.capture.assert_not_called()

    def test_restore_rejects_existing_production_repository_and_symlink_targets(self):
        existing = self.base / "already-there"
        existing.mkdir()
        alias = self.base / "production-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        forbidden = (
            existing,
            self.root,
            self.root / "new-restore",
            self.directory / "new-restore",
            alias,
            alias / "new-restore",
        )
        with patch.object(self.backup, "restic") as restic:
            for destination in forbidden:
                with self.subTest(destination=destination), self.assertRaises(BackupError):
                    self.backup.restore("latest", destination)
        restic.assert_not_called()
        self.assertFalse((self.root / "new-restore").exists())

    def test_restore_validates_snapshot_before_creating_destination(self):
        target = self.base / "isolated-restore"
        with patch.object(self.backup, "restic") as restic:
            for snapshot in ("../../escape", "--option", "latest-extra", "A" * 64, "abc"):
                with self.subTest(snapshot=snapshot), self.assertRaises(BackupError):
                    self.backup.restore(snapshot, target)
                self.assertFalse(target.exists())
        restic.assert_not_called()

    def test_restore_uses_isolated_destination_and_checks_payload_after_restic(self):
        target = self.base / "isolated-restore"
        events = []
        with (
            patch.object(self.backup, "restic", side_effect=lambda *a: events.append(a)) as restic,
            patch(
                "deploy.backup.verify",
                side_effect=lambda p: events.append(p) or {"release_sha": SHA},
            ) as verify,
        ):
            result = self.backup.restore(SNAPSHOT, target)
        self.assertEqual(result, {"state": "verified", "release_sha": SHA})
        self.assertEqual(restic.call_args.args[:2], ("restore", SNAPSHOT))
        self.assertIn("--verify", restic.call_args.args)
        verify.assert_called_once_with(target / "payload")
        self.assertEqual(events[-1], target / "payload")
        self.assertEqual(target.stat().st_mode & 0o077, 0)
        self.assertEqual(self.host.events, [])

    def test_private_directory_and_password_are_enforced(self):
        self.password.chmod(0o644)
        with self.assertRaisesRegex(BackupError, "private backup password"):
            self.backup.prepare()
        self.password.chmod(0o600)
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(BackupError, "Backup directory must be private"):
            self.backup.prepare()

    def test_restic_uses_password_file_argument_never_password_contents(self):
        self.host.command = Mock(return_value="fixture")
        self.assertEqual(self.backup.restic("check"), "fixture")
        argv = self.host.command.call_args.args[0]
        self.assertIn(str(self.password), argv)
        self.assertIn("--password-file", argv)
        self.assertNotIn(SECRET, repr(self.host.command.call_args))


class BackupCommandTests(unittest.TestCase):
    def test_subprocess_errors_do_not_include_output_or_password(self):
        cases = (
            subprocess.CompletedProcess(["restic"], 1, SECRET, SECRET),
            subprocess.TimeoutExpired(["restic", SECRET], 3, output=SECRET, stderr=SECRET),
        )
        for outcome in cases:
            with self.subTest(outcome=type(outcome).__name__):
                kwargs = (
                    {"side_effect": outcome}
                    if isinstance(outcome, Exception)
                    else {"return_value": outcome}
                )
                with patch("deploy.backup.subprocess.run", **kwargs) as run:
                    with self.assertRaises(BackupError) as error:
                        Host().command(["restic", "--password-file", "/fixture/password"])
                self.assertNotIn(SECRET, str(error.exception))
                self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_cli_failure_prints_only_safe_action_and_exception_type(self):
        for action in ("backup", "resume", "restore", "check", "init"):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch("sys.argv", ["backup.py", action]),
                patch("deploy.backup.os.umask"),
                patch("deploy.backup.signal.signal"),
                patch("deploy.backup.Backups") as cls,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                cls.return_value.prepare.side_effect = RuntimeError(SECRET)
                cls.return_value.resume.side_effect = RuntimeError(SECRET)
                self.assertEqual(main(), 1)
            self.assertNotIn(SECRET, stdout.getvalue() + stderr.getvalue())
            self.assertEqual(stderr.getvalue(), f"Backup action {action} failed (RuntimeError)\n")

    def test_cli_resume_does_not_require_backup_preparation(self):
        with (
            patch("sys.argv", ["backup.py", "resume"]),
            patch("deploy.backup.os.umask"),
            patch("deploy.backup.signal.signal"),
            patch("deploy.backup.Backups") as cls,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cls.return_value.prepare.side_effect = BackupError("missing password")
            self.assertEqual(main(), 0)
        cls.return_value.prepare.assert_not_called()
        cls.return_value.resume.assert_called_once()


if __name__ == "__main__":
    unittest.main()
