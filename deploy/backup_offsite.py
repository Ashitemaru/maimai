"""Pull server snapshots into a separately encrypted, local restic repository.

Provision a 0700 configuration directory containing 0600 source-password,
password, ssh_config, and known_hosts files. Password contents stay with restic;
this process never reads them or forwards command output to logs. Run --init once
before scheduling normal copies. No server data or service is changed here.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SOURCE_REPOSITORY = "sftp:mai-journal-backup:/var/backups/mai-journal/repository"
SSH_HOST = "mai-journal-backup"
COMMAND_TIMEOUT = 7200


class BackupError(Exception):
    def __init__(self, code, exit_code=None):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def _private(path, *, directory=False):
    try:
        info = path.lstat()
    except OSError:
        raise BackupError(
            "missing_configuration" if directory else "missing_private_file"
        ) from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (
        not expected(info.st_mode)
        or stat.S_IMODE(info.st_mode) != mode
        or info.st_uid != os.getuid()
    ):
        raise BackupError("unsafe_permissions")
    return info


@contextmanager
def _locked(config):
    descriptor = os.open(
        config / "backup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        _private(config / "backup.lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("already_running") from None
        yield
    finally:
        os.close(descriptor)


def _write_status(config, status):
    destination = config / "status.json"
    if destination.exists() or destination.is_symlink():
        _private(destination)
    descriptor, name = tempfile.mkstemp(prefix=".status-", dir=config)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(status, stream, ensure_ascii=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def _last_success(config):
    path = config / "status.json"
    if not path.exists():
        return None
    if _private(path).st_size > 65536:
        raise BackupError("invalid_status")
    try:
        previous = json.loads(path.read_text(encoding="utf-8")).get("last_success_at")
        if previous is None:
            return None
        parsed = datetime.fromisoformat(previous)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    except (ValueError, TypeError, AttributeError):
        raise BackupError("invalid_status") from None


def _ssh_configuration(config):
    path = config / "ssh_config"
    if _private(path).st_size > 65536:
        raise BackupError("invalid_ssh_config")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        settings = {}
        for line in lines:
            parts = shlex.split(line, comments=True)
            if not parts:
                continue
            if len(parts) != 2:
                raise ValueError
            key, value = parts[0].lower(), parts[1]
            if key in settings:
                raise ValueError
            settings[key] = value
    except (UnicodeError, ValueError):
        raise BackupError("invalid_ssh_config") from None
    required = {
        "host": SSH_HOST,
        "hostname": "47.93.125.83",
        "user": "root",
        "stricthostkeychecking": "yes",
        "batchmode": "yes",
        "identitiesonly": "yes",
        "userknownhostsfile": str(config / "known_hosts"),
        "globalknownhostsfile": "/dev/null",
    }
    optional = {
        "identityfile",
        "connecttimeout",
        "serveraliveinterval",
        "serveralivecountmax",
        "loglevel",
    }
    if (
        next(iter(settings), None) != "host"
        or any(settings.get(key) != value for key, value in required.items())
        or not settings.keys() <= required.keys() | optional
    ):
        raise BackupError("invalid_ssh_config")


class OffsiteBackup:
    def __init__(self, config, repository, restic="restic"):
        self.config = Path(config).expanduser().absolute()
        self.repository = Path(repository).expanduser().absolute()
        self.restic = str(restic)
        self.status = {}

    def _stage(self, stage):
        self.status["stage"] = stage
        _write_status(self.config, self.status)

    def _command(self, *arguments):
        command = [
            self.restic,
            "--repo",
            str(self.repository),
            "--password-file",
            str(self.config / "password"),
            "--quiet",
            "--no-cache",
            "-o",
            "sftp.command="
            + shlex.join(["ssh", "-F", str(self.config / "ssh_config"), SSH_HOST, "-s", "sftp"]),
            *arguments,
        ]
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("RESTIC_")
            and key not in {"DEBUG_LOG", "DEBUG_FILES", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}
        }
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise BackupError("restic_timeout") from None
        except OSError:
            raise BackupError("restic_unavailable") from None
        if result.returncode:
            raise BackupError("restic_failed", result.returncode)

    def _validate(self):
        for name in ("password", "source-password", "known_hosts"):
            if not _private(self.config / name).st_size:
                raise BackupError("empty_private_file")
        _ssh_configuration(self.config)
        if self.repository == self.config:
            raise BackupError("invalid_repository")
        self.repository.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private(self.repository, directory=True)

    def run(self, *, initialize=False):
        _private(self.config, directory=True)
        with _locked(self.config):
            self.status = {
                "schema_version": 1,
                "operation": "init" if initialize else "copy",
                "state": "running",
                "stage": "preflight",
                "started_at": _now(),
                "finished_at": None,
                "last_success_at": _last_success(self.config),
            }
            try:
                self._stage("preflight")
                self._validate()
                repository_config = self.repository / "config"
                if initialize:
                    self._stage("init")
                    if repository_config.is_file() and not repository_config.is_symlink():
                        self._command("cat", "config")
                    else:
                        if any(self.repository.iterdir()):
                            raise BackupError("repository_not_empty")
                        self._command(
                            "init",
                            "--from-repo",
                            SOURCE_REPOSITORY,
                            "--from-password-file",
                            str(self.config / "source-password"),
                            "--copy-chunker-params",
                        )
                    self.status["state"] = "initialized"
                else:
                    if not repository_config.is_file() or repository_config.is_symlink():
                        raise BackupError("repository_not_initialized")
                    self._stage("copy")
                    self._command(
                        "copy",
                        "--from-repo",
                        SOURCE_REPOSITORY,
                        "--from-password-file",
                        str(self.config / "source-password"),
                    )
                    self._stage("check")
                    self._command("check")
                    self._stage("forget")
                    self._command(
                        "forget",
                        "--group-by",
                        "",
                        "--keep-daily",
                        "7",
                        "--keep-weekly",
                        "4",
                        "--keep-monthly",
                        "6",
                    )
                    self._stage("prune")
                    self._command("prune")
                    self.status["last_success_at"] = _now()
                    self.status["state"] = "succeeded"
                self.status["stage"] = "completed"
            except BackupError as error:
                self.status.update(state="failed", error=error.code)
                if error.exit_code is not None:
                    self.status["exit_code"] = error.exit_code
            except (OSError, ValueError):
                self.status.update(state="failed", error="local_io_failed")
            except KeyboardInterrupt:
                self.status.update(state="failed", error="interrupted")
            self.status["finished_at"] = _now()
            _write_status(self.config, self.status)
            return self.status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--restic", default="restic")
    parser.add_argument("--init", action="store_true", help="Initialize the local repository only")
    args = parser.parse_args(argv)
    try:
        status = OffsiteBackup(args.config, args.repository, args.restic).run(initialize=args.init)
    except BackupError as error:
        status = {
            "state": "skipped" if error.code == "already_running" else "failed",
            "error": error.code,
        }
    except OSError:
        status = {"state": "failed", "error": "local_io_failed"}
    print(json.dumps({key: status[key] for key in ("state", "stage", "error") if key in status}))
    if status["state"] == "skipped":
        return 75
    return 0 if status["state"] in {"succeeded", "initialized"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
