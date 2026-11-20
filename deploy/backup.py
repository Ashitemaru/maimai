#!/usr/bin/env python3
"""Daily encrypted backup of mai journal data with restic; the app keeps running.

SQLite files are copied with the online backup API, so each database is consistent
on its own. Catalog snapshots are immutable directories; the current and previous
ones are copied while holding the catalog publish lock. Code is not backed up: every
deployed commit has a GitHub release. The Mac offsite job copies snapshots of this
repository (host mai-journal, tag daily), so keep those names and the repository path.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DATABASES = (
    ("shared/runtime/personal.sqlite3", True),
    ("shared/catalog/updates/adjudications.sqlite3", True),
    ("shared/runtime/auth.sqlite3", False),
)
RUNTIME_FILES = ("shared/runtime/secrets.json", "shared/runtime/bilibili-session.json")
CATALOG_EXCLUDED = {"current", "updates", "remy-cache"}
SYSTEM_FILES = ("etc/mai-journal/app.env", "etc/caddy/Caddyfile", "etc/sudoers.d/mai-journal")
UNIT = re.compile(r"mai-journal(?:-[a-z0-9-]+)?\.(?:service|timer)\Z")
SNAPSHOT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
RESTIC_SCOPE = ("--host", "mai-journal", "--tag", "daily")


class BackupError(Exception):
    pass


def atomic_json(path, value):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


@contextmanager
def lock(path):
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def copy_database(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dest:
            src.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError(f"Integrity check failed for {source.name}")


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def catalog_snapshots(root):
    """Return (current link text, snapshot names) for the current and previous catalog."""
    catalog = root / "shared/catalog"
    link = os.readlink(catalog / "current")
    parts = Path(link).parts
    if len(parts) != 3 or parts[:2] != ("updates", "snapshots") or not SNAPSHOT.fullmatch(parts[2]):
        raise BackupError("Unexpected catalog current pointer")
    names = [parts[2]]
    status = catalog / "updates/status.json"
    previous = json.loads(status.read_text()).get("previous_snapshot") if status.exists() else None
    if isinstance(previous, str) and SNAPSHOT.fullmatch(previous) and previous != names[0]:
        if (catalog / "updates/snapshots" / previous).is_dir():
            names.append(previous)
    return link, names


def capture(root, payload, *, system_root=Path("/")):
    """Copy everything worth restoring into payload; return a small manifest."""
    for relative, required in DATABASES:
        source = root / relative
        if source.is_file():
            copy_database(source, payload / relative)
        elif required:
            raise BackupError(f"Missing required database {relative}")
    for relative in RUNTIME_FILES:
        if (root / relative).is_file():
            copy_file(root / relative, payload / relative)
    catalog = root / "shared/catalog"
    with lock(catalog / "updates/publish.lock"):
        link, snapshots = catalog_snapshots(root)
        for item in catalog.iterdir():
            if item.name in CATALOG_EXCLUDED or item.is_symlink():
                continue
            target = payload / "shared/catalog" / item.name
            if item.is_dir():
                shutil.copytree(item, target, symlinks=True)
            else:
                copy_file(item, target)
        status = catalog / "updates/status.json"
        if status.is_file():
            copy_file(status, payload / "shared/catalog/updates/status.json")
        for name in snapshots:
            relative = f"shared/catalog/updates/snapshots/{name}"
            shutil.copytree(root / relative, payload / relative, symlinks=True)
        (payload / "shared/catalog/current").symlink_to(link)
    shutil.copytree(root / "shared/assets", payload / "shared/assets", symlinks=True)
    for relative in SYSTEM_FILES:
        if (system_root / relative).is_file():
            copy_file(system_root / relative, payload / "system" / relative)
    units = system_root / "etc/systemd/system"
    for unit in sorted(units.iterdir()) if units.is_dir() else ():
        if UNIT.fullmatch(unit.name) and unit.is_file():
            copy_file(unit, payload / "system/etc/systemd/system" / unit.name)
    current = root / "current"
    manifest = {
        "schema_version": 4,
        "created_at": datetime.now(UTC).isoformat(),
        "release_sha": current.resolve().name if current.is_symlink() else None,
        "catalog_snapshots": snapshots,
    }
    atomic_json(payload / "manifest.json", manifest)
    return manifest


def verify(payload):
    """Check a restored payload: manifest present and every database intact."""
    manifest = json.loads((payload / "manifest.json").read_text())
    for relative, required in DATABASES:
        path = payload / relative
        if not path.is_file():
            if required:
                raise BackupError(f"Restored payload lacks {relative}")
            continue
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError(f"Restored {relative} failed its integrity check")
    for name in manifest.get("catalog_snapshots", []):
        if not (payload / "shared/catalog/updates/snapshots" / name / "catalog.sqlite").is_file():
            raise BackupError(f"Restored payload lacks catalog snapshot {name}")
    return manifest


class Backups:
    def __init__(
        self, root, directory, password, *, restic="restic", runner=subprocess.run, system_root="/"
    ):
        self.root = Path(root)
        self.directory = Path(directory)
        self.repository = self.directory / "repository"
        self.staging = self.directory / "staging"
        self.status_path = self.directory / "status.json"
        self.password = Path(password)
        self.restic_binary = restic
        self.runner = runner
        self.system_root = Path(system_root)

    def restic(self, *arguments, cwd=None):
        command = [
            self.restic_binary,
            "--repo",
            str(self.repository),
            "--password-file",
            str(self.password),
            "--cache-dir",
            str(self.directory / "cache"),
            *arguments,
        ]
        result = self.runner(command, cwd=cwd, capture_output=True, text=True, timeout=3600)
        if result.returncode:
            raise BackupError(f"restic {arguments[0]} failed: {result.stderr.strip()[-300:]}")
        return result.stdout

    def backup(self, *, full_check=False):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with lock(self.directory / "backup.lock"):
            if not (self.repository / "config").exists():
                raise BackupError("Run `backup.py init` before scheduling backups")
            previous = json.loads(self.status_path.read_text()) if self.status_path.exists() else {}
            status = {
                "state": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "last_success": previous.get("last_success"),
            }
            atomic_json(self.status_path, status)
            try:
                shutil.rmtree(self.staging, ignore_errors=True)
                self.staging.mkdir(mode=0o700)
                manifest = capture(
                    self.root, self.staging / "payload", system_root=self.system_root
                )
                output = self.restic("backup", "--json", *RESTIC_SCOPE, "payload", cwd=self.staging)
                lines = [json.loads(line) for line in output.splitlines() if line.strip()]
                summary = next((v for v in lines if v.get("message_type") == "summary"), {})
                self.restic("check", "--read-data" if full_check else "--read-data-subset=1/12")
                self.restic(
                    "forget",
                    *RESTIC_SCOPE,
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
                status = {
                    "state": "complete",
                    "last_success": datetime.now(UTC).isoformat(),
                    "snapshot_id": summary.get("snapshot_id"),
                    "release_sha": manifest["release_sha"],
                }
            except Exception as error:
                status.update(state="failed", error=str(error)[:500])
                raise
            finally:
                status["finished_at"] = datetime.now(UTC).isoformat()
                atomic_json(self.status_path, status)
                # Plaintext staging is disposable; encrypted snapshots remain.
                shutil.rmtree(self.staging, ignore_errors=True)

    def restore(self, snapshot, destination):
        destination = Path(destination).absolute()
        if destination.exists():
            raise BackupError("Restore requires a new destination path")
        for forbidden in (self.root, self.directory):
            if destination.resolve().is_relative_to(forbidden.resolve()):
                raise BackupError("Restore destination must be outside app and backup paths")
        destination.mkdir(mode=0o700, parents=True)
        self.restic("restore", snapshot, *RESTIC_SCOPE, "--target", str(destination), "--verify")
        return verify(destination / "payload")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "backup", "restore", "check"])
    parser.add_argument("--root", type=Path, default=Path("/srv/mai-journal"))
    parser.add_argument("--directory", type=Path, default=Path("/var/backups/mai-journal"))
    parser.add_argument("--password", type=Path, default=Path("/etc/mai-journal/backup-password"))
    parser.add_argument("--restic", default="restic")
    parser.add_argument("--full-check", action="store_true")
    parser.add_argument("--snapshot", default="latest")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    backups = Backups(args.root, args.directory, args.password, restic=args.restic)
    try:
        if args.action == "init":
            if not (backups.repository / "config").exists():
                backups.restic("init")
        elif args.action == "backup":
            first_of_month = datetime.now(ZoneInfo("Asia/Shanghai")).day == 1
            backups.backup(full_check=args.full_check or first_of_month)
        elif args.action == "check":
            backups.restic("check", "--read-data")
        else:
            if not args.destination:
                parser.error("restore requires --destination")
            manifest = backups.restore(args.snapshot, args.destination)
            print(f"Restored and verified backup of release {manifest.get('release_sha')}")
    except (BackupError, OSError, sqlite3.Error, ValueError) as error:
        print(f"Backup action {args.action} failed: {error}", file=sys.stderr)
        return 1
    print(f"Backup action {args.action} completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
