import contextlib
import fcntl
import io
import json
import os
import plistlib
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.backup_offsite import SOURCE_REPOSITORY, OffsiteBackup, main

SECRET = "synthetic-private-password-never-log"


class OffsiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = self.base / "private config's files"
        self.config.mkdir(mode=0o700)
        self.repository = self.base / "local repository"
        self.repository.mkdir(mode=0o700)
        (self.repository / "config").write_text("synthetic encrypted repository config")
        for name in ("source-password", "password", "known_hosts"):
            self.write_private(name, SECRET)
        self.ssh_config = (
            "Host mai-journal-backup\n"
            "  HostName 47.93.125.83\n"
            "  User root\n"
            f"  UserKnownHostsFile {json.dumps(str(self.config / 'known_hosts'))}\n"
            "  GlobalKnownHostsFile /dev/null\n"
            "  StrictHostKeyChecking yes\n"
            "  BatchMode yes\n"
            "  IdentitiesOnly yes\n"
            "  IdentityFile /private/synthetic-key\n"
            "  ConnectTimeout 15\n"
        )
        self.write_private("ssh_config", self.ssh_config)
        self.calls = []
        self.failure = None
        self.fail_with = None

    def write_private(self, name, value):
        path = self.config / name
        path.write_text(value)
        path.chmod(0o600)

    def restic(self, command, **kwargs):
        operation = next(
            item for item in command if item in {"init", "cat", "copy", "check", "forget", "prune"}
        )
        self.calls.append((operation, command, kwargs))
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(kwargs["check"])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn(SECRET, repr(command))
        self.assertNotIn(SECRET, repr(kwargs["env"]))
        self.assertFalse(any(key.startswith("RESTIC_") for key in kwargs["env"]))
        if self.failure == operation:
            if self.fail_with:
                raise self.fail_with
            return subprocess.CompletedProcess(command, 12, stdout=SECRET, stderr=SECRET)
        if operation == "init":
            (self.repository / "config").write_text("new synthetic encrypted repository config")
        return subprocess.CompletedProcess(command, 0, stdout=SECRET, stderr=SECRET)

    def run_backup(self, *, initialize=False):
        output = io.StringIO()
        args = [
            "--config",
            str(self.config),
            "--repository",
            str(self.repository),
            "--restic",
            "/synthetic/restic",
        ]
        if initialize:
            args.append("--init")
        with (
            patch("deploy.backup_offsite.subprocess.run", side_effect=self.restic),
            patch.dict(os.environ, {"RESTIC_PASSWORD": SECRET, "RESTIC_FROM_PASSWORD": SECRET}),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            code = main(args)
        logged = output.getvalue()
        self.assertNotIn(SECRET, logged)
        self.assertNotIn(str(self.repository), logged)
        self.assertNotIn(str(self.config), logged)
        self.assertNotIn("synthetic-key", logged)
        return code, json.loads(logged)

    def status(self):
        path = self.config / "status.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        raw = path.read_text()
        self.assertNotIn(SECRET, raw)
        self.assertNotIn(str(self.config), raw)
        self.assertNotIn(str(self.repository), raw)
        return json.loads(raw)

    def test_copy_uses_fixed_source_and_retains_local_snapshots_only(self):
        code, summary = self.run_backup()
        self.assertEqual(code, 0)
        self.assertEqual(summary["state"], "succeeded")
        self.assertEqual([item[0] for item in self.calls], ["copy", "check", "forget", "prune"])
        for _, command, _ in self.calls:
            self.assertEqual(command[command.index("--repo") + 1], str(self.repository))
            self.assertEqual(
                command[command.index("--password-file") + 1], str(self.config / "password")
            )
            ssh = command[command.index("-o") + 1].removeprefix("sftp.command=")
            self.assertEqual(
                shlex.split(ssh),
                ["ssh", "-F", str(self.config / "ssh_config"), "mai-journal-backup", "-s", "sftp"],
            )
        copy = self.calls[0][1]
        self.assertEqual(copy[copy.index("--from-repo") + 1], SOURCE_REPOSITORY)
        self.assertEqual(
            copy[copy.index("--from-password-file") + 1], str(self.config / "source-password")
        )
        forget = self.calls[2][1]
        self.assertNotIn("--from-repo", forget)
        self.assertEqual(
            forget[forget.index("forget") + 1 :],
            ["--group-by", "", "--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6"],
        )
        self.assertIsNotNone(self.status()["last_success_at"])

    def test_copy_failure_never_forgets_or_prunes_and_keeps_last_success_distinct(self):
        self.write_private(
            "status.json", json.dumps({"last_success_at": "2026-10-13T00:00:00+00:00"})
        )
        self.failure = "copy"
        code, summary = self.run_backup()
        self.assertEqual(code, 1)
        self.assertEqual(summary["state"], "failed")
        self.assertEqual([item[0] for item in self.calls], ["copy"])
        status = self.status()
        self.assertEqual(status["stage"], "copy")
        self.assertEqual(status["exit_code"], 12)
        self.assertEqual(status["last_success_at"], "2026-10-13T00:00:00+00:00")

    def test_forget_failure_never_prunes(self):
        self.failure = "forget"
        code, _ = self.run_backup()
        self.assertEqual(code, 1)
        self.assertEqual([item[0] for item in self.calls], ["copy", "check", "forget"])
        self.assertEqual(self.status()["stage"], "forget")
        self.assertIsNone(self.status()["last_success_at"])

    def test_check_failure_never_forgets_or_prunes(self):
        self.failure = "check"
        code, summary = self.run_backup()
        self.assertEqual(code, 1)
        self.assertEqual(summary["state"], "failed")
        self.assertEqual([item[0] for item in self.calls], ["copy", "check"])
        self.assertEqual(self.status()["stage"], "check")
        self.assertIsNone(self.status()["last_success_at"])

    def test_prune_failure_is_not_reported_as_success(self):
        self.failure = "prune"
        code, summary = self.run_backup()
        self.assertEqual(code, 1)
        self.assertEqual(summary["state"], "failed")
        self.assertEqual(self.status()["stage"], "prune")
        self.assertIsNone(self.status()["last_success_at"])

    def test_init_copies_chunker_parameters_and_does_not_copy_or_prune(self):
        (self.repository / "config").unlink()
        code, summary = self.run_backup(initialize=True)
        self.assertEqual(code, 0)
        self.assertEqual(summary["state"], "initialized")
        self.assertEqual([item[0] for item in self.calls], ["init"])
        command = self.calls[0][1]
        self.assertEqual(command[command.index("--from-repo") + 1], SOURCE_REPOSITORY)
        self.assertIn("--copy-chunker-params", command)
        self.assertIn(str(self.config / "source-password"), command)
        self.assertIsNone(self.status()["last_success_at"])

    def test_init_is_idempotent_and_checks_existing_repository_password(self):
        code, _ = self.run_backup(initialize=True)
        self.assertEqual(code, 0)
        self.assertEqual([item[0] for item in self.calls], ["cat"])
        self.failure = "cat"
        self.calls.clear()
        code, summary = self.run_backup(initialize=True)
        self.assertEqual(code, 1)
        self.assertEqual(summary["state"], "failed")
        self.assertEqual([item[0] for item in self.calls], ["cat"])

    def test_init_failure_never_reports_initialized(self):
        (self.repository / "config").unlink()
        self.failure = "init"
        code, summary = self.run_backup(initialize=True)
        self.assertEqual(code, 1)
        self.assertEqual(summary["state"], "failed")
        self.assertEqual([item[0] for item in self.calls], ["init"])

    def test_init_refuses_nonempty_directory_without_repository_config(self):
        (self.repository / "config").rename(self.repository / "unrelated-data")
        code, summary = self.run_backup(initialize=True)
        self.assertEqual(code, 1)
        self.assertEqual(summary["error"], "repository_not_empty")
        self.assertEqual(self.calls, [])

    def test_normal_run_does_not_initialize_a_missing_repository(self):
        (self.repository / "config").unlink()
        code, summary = self.run_backup()
        self.assertEqual(code, 1)
        self.assertEqual(summary["error"], "repository_not_initialized")
        self.assertEqual(self.calls, [])

    def test_lock_contention_does_not_overwrite_active_status(self):
        self.write_private("status.json", '{"state":"running"}\n')
        self.write_private("backup.lock", "")
        with (self.config / "backup.lock").open("r+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, summary = self.run_backup()
        self.assertEqual(code, 75)
        self.assertEqual(summary, {"state": "skipped", "error": "already_running"})
        self.assertEqual(self.status(), {"state": "running"})
        self.assertEqual(self.calls, [])

    def test_timeout_and_executable_errors_are_redacted(self):
        self.failure = "copy"
        for error, expected in [
            (
                subprocess.TimeoutExpired([SECRET], 10, output=SECRET, stderr=SECRET),
                "restic_timeout",
            ),
            (OSError(SECRET), "restic_unavailable"),
        ]:
            with self.subTest(expected=expected):
                self.calls.clear()
                self.fail_with = error
                code, summary = self.run_backup()
                self.assertEqual(code, 1)
                self.assertEqual(summary["error"], expected)
                self.assertEqual([item[0] for item in self.calls], ["copy"])

    def test_world_readable_password_is_rejected_before_running_restic(self):
        (self.config / "source-password").chmod(0o644)
        code, summary = self.run_backup()
        self.assertEqual(code, 1)
        self.assertEqual(summary["error"], "unsafe_permissions")
        self.assertEqual(self.calls, [])

    def test_symlinked_credentials_are_rejected(self):
        (self.config / "source-password").unlink()
        (self.config / "source-password").symlink_to(self.config / "password")
        code, summary = self.run_backup()
        self.assertEqual(code, 1)
        self.assertEqual(summary["error"], "unsafe_permissions")
        self.assertEqual(self.calls, [])

    def test_ssh_target_or_external_configuration_cannot_override_fixed_source(self):
        for config in [
            self.ssh_config.replace("47.93.125.83", "wrong.example"),
            self.ssh_config.replace("User root", "User other"),
            self.ssh_config.replace("StrictHostKeyChecking yes", "StrictHostKeyChecking no"),
            self.ssh_config + "Include /private/other-config\n",
            self.ssh_config + "ProxyCommand forbidden\n",
        ]:
            with self.subTest(config=config):
                self.write_private("ssh_config", config)
                code, summary = self.run_backup()
                self.assertEqual(code, 1)
                self.assertEqual(summary["error"], "invalid_ssh_config")
        self.assertEqual(self.calls, [])

    def test_password_contents_are_not_read_by_python(self):
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name in {"password", "source-password"}:
                self.fail("Python must not read either repository password")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            code, _ = self.run_backup()
        self.assertEqual(code, 0)

    def test_private_repository_and_status_are_created_for_initialization(self):
        (self.repository / "config").unlink()
        self.repository.rmdir()
        backup = OffsiteBackup(self.config, self.repository, "/synthetic/restic")
        with patch("deploy.backup_offsite.subprocess.run", side_effect=self.restic):
            status = backup.run(initialize=True)
        self.assertEqual(status["state"], "initialized")
        self.assertEqual(stat.S_IMODE(self.repository.stat().st_mode), 0o700)
        self.status()

    def test_launch_agent_is_hourly_and_runs_without_init(self):
        source = Path(__file__).resolve().parents[1] / "deploy/com.maijournal.backup.plist.example"
        settings = plistlib.loads(source.read_bytes())
        self.assertTrue(settings["RunAtLoad"])
        self.assertEqual(settings["StartInterval"], 3600)
        self.assertEqual(settings["Umask"], 0o77)
        self.assertNotIn("--init", settings["ProgramArguments"])
        self.assertIn("--config", settings["ProgramArguments"])
        self.assertIn("--repository", settings["ProgramArguments"])


if __name__ == "__main__":
    unittest.main()
