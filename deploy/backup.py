#!/usr/bin/env python3
"""Encrypted, consistent backups and isolated restore verification for mai journal."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .backup_data import capture, verify
else:
    from backup_data import capture, verify

SERVICE = "mai-journal.service"
READY_URL = "http://127.0.0.1:8766/api/ready"


class BackupError(RuntimeError):
    """Safe operational message, never subprocess output or credential contents."""


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextlib.contextmanager
def lock(path):
    # Never unlink lock files: deployment and catalog writers share their inodes.
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("Another deployment, publication or backup is running") from None
        yield


class Host:
    def command(self, argv, *, cwd=None, timeout=3600):
        try:
            result = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            raise BackupError(f"{Path(argv[0]).name} timed out") from None
        if result.returncode:
            raise BackupError(f"{Path(argv[0]).name} failed (exit {result.returncode})")
        return result.stdout

    def state(self):
        return self.command(
            ["systemctl", "show", SERVICE, "--property=ActiveState", "--value"], timeout=30
        ).strip()

    def service(self, action):
        self.command(["systemctl", action, SERVICE], timeout=270)

    def quiescent(self):
        return (
            self.command(
                ["systemctl", "show", SERVICE, "--property=MainPID", "--value"], timeout=30
            ).strip()
            == "0"
        )

    def ready(self, sha, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(READY_URL, timeout=3) as response:
                    value = json.load(response)
                if value.get("ok") is True and value.get("release_sha") == sha:
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1)
        raise BackupError("Application did not become ready after backup capture")


class Backups:
    def __init__(self, root, directory, password, *, config=None, host=None, restic="restic"):
        self.root = Path(root)
        self.directory = Path(directory)
        self.password = Path(password)
        self.config = Path(config or "/etc/mai-journal")
        self.host = host or Host()
        self.restic_binary = restic
        self.repository = self.directory / "repository"
        self.staging = self.directory / "staging"
        self.marker = self.directory / "resume-needed.json"
        self.receipt = self.directory / "status.json"

    def restic(self, *arguments, cwd=None):
        return self.host.command(
            [
                self.restic_binary,
                "--repo",
                str(self.repository),
                "--password-file",
                str(self.password),
                "--cache-dir",
                str(self.directory / "cache"),
                *arguments,
            ],
            cwd=cwd,
        )

    def prepare(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise BackupError("Backup directory must be private and not a symbolic link")
        if (
            not self.password.is_file()
            or self.password.is_symlink()
            or self.password.stat().st_mode & 0o077
        ):
            raise BackupError("A private backup password file is required")

    def current_sha(self):
        sha = (self.root / "current/RELEASE_SHA").read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise BackupError("Current release has an invalid identity")
        return sha

    def restore_service(self):
        if not self.marker.exists():
            return
        # Called with deploy.lock held. A completed deployment can have changed SHA.
        if (self.root / "deployment-pending.json").exists():
            raise BackupError("Deployment recovery must finish before resuming backups")
        self.host.service("start")
        self.host.ready(self.current_sha())
        self.marker.unlink()

    def resume(self):
        # Restarting the app must not depend on a healthy restic key or repository.
        if self.directory.is_symlink():
            raise BackupError("Invalid backup directory")
        if self.marker.exists():
            with lock(self.root / "deploy.lock"):
                self.restore_service()

    def snapshot(self):
        with lock(self.root / "deploy.lock"):
            self.restore_service()
            if (self.root / "deployment-pending.json").exists():
                raise BackupError("Deployment recovery is pending; backup capture postponed")
            state = self.host.state()
            if state not in {"active", "inactive", "failed"}:
                raise BackupError("Application is changing state; backup capture postponed")
            self.staging.mkdir(exist_ok=True, mode=0o700)
            payload = self.staging / "payload"
            if payload.exists():
                shutil.rmtree(payload)
            try:
                if state == "active":
                    atomic_json(self.marker, {"release_sha": self.current_sha()})
                    self.host.service("stop")
                if self.host.state() not in {"inactive", "failed"} or not self.host.quiescent():
                    raise BackupError("Application is still running; no snapshot captured")
                with lock(self.root / "shared/catalog/updates/publish.lock"):
                    manifest = capture(self.root, self.config, payload)
            finally:
                self.restore_service()
        return manifest

    def backup(self, *, full_check=False):
        self.prepare()
        with lock(self.directory / "backup.lock"):
            if not (self.repository / "config").exists():
                raise BackupError("Initialize the encrypted repository before scheduling backups")
            previous = json.loads(self.receipt.read_text()) if self.receipt.exists() else {}
            status = {
                "state": "running",
                "phase": "capture",
                "started_at": datetime.now(UTC).isoformat(),
                "last_success": previous.get("last_success"),
            }
            atomic_json(self.receipt, status)
            try:
                captured_at = time.monotonic()
                manifest = self.snapshot()
                status.update(
                    phase="encrypt", capture_seconds=round(time.monotonic() - captured_at, 2)
                )
                atomic_json(self.receipt, status)
                result = self.restic(
                    "backup",
                    "--json",
                    "--host",
                    "mai-journal",
                    "--tag",
                    "daily",
                    "payload",
                    cwd=self.staging,
                )
                summaries = [json.loads(line) for line in result.splitlines() if line.strip()]
                snapshot = next(
                    (v.get("snapshot_id") for v in summaries if v.get("message_type") == "summary"),
                    None,
                )
                if not snapshot:
                    raise BackupError("Backup completed without a snapshot receipt")
                status.update(phase="check", snapshot_id=snapshot)
                atomic_json(self.receipt, status)
                self.restic("check", "--read-data" if full_check else "--read-data-subset=1/12")
                status["phase"] = "retention"
                atomic_json(self.receipt, status)
                self.restic(
                    "forget",
                    "--host",
                    "mai-journal",
                    "--tag",
                    "daily",
                    "--group-by",
                    "host,tags",
                    "--keep-daily",
                    "7",
                    "--keep-weekly",
                    "4",
                    "--keep-monthly",
                    "6",
                    "--prune",
                )
                status.update(
                    state="complete",
                    phase="complete",
                    last_success=datetime.now(UTC).isoformat(),
                    snapshot_id=snapshot,
                    release_sha=manifest.get("release_sha"),
                )
                return status
            except Exception as error:
                status.update(
                    state="failed", error=f"{type(error).__name__}: backup did not finish"
                )
                raise
            finally:
                status["finished_at"] = datetime.now(UTC).isoformat()
                atomic_json(self.receipt, status)
                # Plaintext staging is disposable; encrypted snapshots remain untouched.
                if self.staging.exists():
                    shutil.rmtree(self.staging)

    def restore(self, snapshot, destination):
        self.prepare()
        destination = Path(destination).absolute()
        if destination.exists() or destination.is_symlink():
            raise BackupError("Restore requires a new, empty destination path")
        if not re.fullmatch(r"[0-9a-f]{8,64}|latest", snapshot):
            raise BackupError("Invalid snapshot identity")
        # Never restore within production or into the backup repository itself.
        for forbidden in (self.root.resolve(), self.directory.resolve()):
            if destination.resolve().is_relative_to(forbidden):
                raise BackupError(
                    "Restore destination must be outside application and backup paths"
                )
        destination.mkdir(mode=0o700, parents=True)
        self.restic(
            "restore",
            snapshot,
            "--host",
            "mai-journal",
            "--tag",
            "daily",
            "--target",
            str(destination),
            "--verify",
        )
        manifest = verify(destination / "payload")
        return {"state": "verified", "release_sha": manifest.get("release_sha")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "backup", "resume", "restore", "check"])
    parser.add_argument("--root", type=Path, default=Path("/srv/mai-journal"))
    parser.add_argument("--directory", type=Path, default=Path("/var/backups/mai-journal"))
    parser.add_argument("--password", type=Path, default=Path("/etc/mai-journal/backup-password"))
    parser.add_argument("--config", type=Path, default=Path("/etc/mai-journal"))
    parser.add_argument("--restic", default="restic")
    parser.add_argument("--full-check", action="store_true")
    parser.add_argument("--snapshot", default="latest")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    os.umask(0o077)

    def terminate(signum, frame):
        raise BackupError("Backup interrupted")

    signal.signal(signal.SIGTERM, terminate)
    backup = Backups(
        args.root, args.directory, args.password, config=args.config, restic=args.restic
    )
    try:
        if args.action != "resume":
            backup.prepare()
        if args.action == "init":
            with lock(backup.directory / "backup.lock"):
                if not (backup.repository / "config").exists():
                    backup.restic("init")
        elif args.action == "backup":
            backup.backup(full_check=args.full_check or datetime.now(UTC).day == 1)
        elif args.action == "resume":
            backup.resume()
        elif args.action == "check":
            backup.restic("check", "--read-data")
        elif args.action == "restore":
            if not args.destination:
                parser.error("restore requires --destination")
            backup.restore(args.snapshot, args.destination)
    except Exception as error:
        # Do not log credentials, filenames inside personal data, or CLI output.
        print(f"Backup action {args.action} failed ({type(error).__name__})", file=sys.stderr)
        return 1
    print(f"Backup action {args.action} completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
