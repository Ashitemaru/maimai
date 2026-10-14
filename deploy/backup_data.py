"""Copy quiesced application data, then validate the independent copy while online.

capture_frozen requires deploy.lock, a stopped application and publish.lock.
finalize_capture reads only the private copy, so those locks and the application
pause can end before file fsyncs, database checks and two full checksum passes.
Only the current and immediately previous catalog snapshots are retained. Older
history entries in status.json are informational and can refer to omitted snapshots.
This module never restores over a running installation or contacts the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

SHA = re.compile(r"[0-9a-f]{40}\Z")
SNAPSHOT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
UNIT = re.compile(r"mai-journal(?:-[a-z0-9-]+)?@?\.(?:service|timer|socket)\Z")
DB_PATHS = (
    "shared/runtime/personal.sqlite3",
    "shared/catalog/updates/adjudications.sqlite3",
    "shared/runtime/auth.sqlite3",
)
CONFIG_PATHS = ("config/mai-journal/app.env", "config/mai-journal/deploy.env")
LEGACY_OPTIONAL_PATHS = (
    "shared/runtime/auth.sqlite3",
    "shared/runtime/secrets.json",
    "config/system/etc/caddy/Caddyfile",
    "config/system/etc/sudoers.d/mai-journal",
)
WAHLAP_PROXY_SYSTEM_PATHS = (
    "etc/mai-journal-wahlap-proxy/egress.json",
    "etc/mai-journal-wahlap-proxy/tun.json",
    "etc/netns/mai-wahlap/resolv.conf",
    "etc/systemd/system/mai-journal-wahlap@.service.d/proxy.conf",
)
WAHLAP_PROXY_PATHS = tuple("config/system/" + path for path in WAHLAP_PROXY_SYSTEM_PATHS)
OPTIONAL_PATHS = (*LEGACY_OPTIONAL_PATHS, *WAHLAP_PROXY_PATHS)
BILIBILI_SESSION_PATH = "shared/runtime/bilibili-session.json"


class BackupDataError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise BackupDataError(message)


def _relative(value):
    _require(isinstance(value, str) and value and "\\" not in value, "Invalid backup path")
    parts = PurePosixPath(value).parts
    _require(
        not value.startswith("/")
        and all(part not in (".", "..") for part in value.split("/"))
        and all(part for part in value.split("/"))
        and not any(ord(char) < 32 or ord(char) == 127 for char in value),
        "Invalid backup path",
    )
    return Path(*parts)


def _path(root, relative, *, missing=False):
    """Reject links in every component, including source parent directories."""
    target = root
    parts = _relative(relative).parts
    for index, part in enumerate(parts):
        target /= part
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            if missing:
                return target.joinpath(*parts[index + 1 :])
            raise BackupDataError("Required backup input is missing") from None
        _require(not stat.S_ISLNK(mode), "Unexpected symbolic link in backup data")
        if index < len(parts) - 1:
            _require(stat.S_ISDIR(mode), "Invalid backup parent directory")
    return target


def _directory(path):
    _require(path.is_dir() and not path.is_symlink(), "Required directory is missing or linked")


def _file(path):
    _require(stat.S_ISREG(path.lstat().st_mode), "Expected a regular backup file")


def _json(path):
    _file(path)
    _require(path.stat().st_size <= 64 * 1024 * 1024, "Backup JSON is too large")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, "Duplicate backup JSON key")
            value[key] = item
        return value

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError):
        raise BackupDataError("Invalid backup JSON") from None
    _require(isinstance(value, dict), "Expected a backup JSON object")
    return value


def _hash(path, *, sync=False):
    with path.open("rb") as stream:
        if sync:
            os.fsync(stream.fileno())
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _temporary(name):
    return (
        name.endswith((".tmp", ".part", ".pyc", "-wal", "-shm", "-journal"))
        or name.endswith(".lock")
        and name != "requirements.lock"
        or name.startswith((".current-", ".secrets-"))
        or name in {"__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store"}
    )


def _copy_file(source, destination):
    _file(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # O_NOFOLLOW avoids following a final-component symlink swapped after lstat.
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as incoming, destination.open("xb") as outgoing:
        mode = os.fstat(incoming.fileno()).st_mode
        _require(stat.S_ISREG(mode), "Expected a regular backup file")
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        outgoing.flush()
        os.fchmod(outgoing.fileno(), 0o700 if mode & 0o111 else 0o600)
        # Closing makes this copy independent of the source. Durability waits
        # until finalization after the app resumes; incomplete staging is disposable.


def _copy_tree(source, destination, *, excluded=()):
    _directory(source)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for child in sorted(source.iterdir()):
        if child.name in excluded or _temporary(child.name):
            continue
        _require(not child.is_symlink(), "Unexpected symbolic link in backup data")
        target = destination / child.name
        if child.is_dir():
            _copy_tree(child, target)
        else:
            _copy_file(child, target)


def _database_report(path):
    _file(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        _require(
            connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
            "SQLite backup integrity check failed",
        )
        _require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "SQLite FK error")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        return {
            "tables": {
                name: connection.execute(
                    'SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"'
                ).fetchone()[0]
                for name in tables
            },
            "integrity": "ok",
        }


def _backup_database(source, destination):
    _file(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as incoming:
        with closing(sqlite3.connect(destination)) as outgoing:
            incoming.backup(outgoing)
            # backup() carries the source WAL setting. Normalize this standalone
            # copy so later verification/restoration never requires sidecar files.
            _require(
                outgoing.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete",
                "SQLite backup could not become a standalone file",
            )
    destination.chmod(0o600)


def _code_release(root, *, allow_absolute=False):
    pointer = root / "current"
    _require(pointer.is_symlink(), "Application current must be a release symlink")
    target = os.readlink(pointer)
    if allow_absolute and Path(target).is_absolute():
        try:
            target = Path(target).relative_to(root).as_posix()
        except ValueError:
            raise BackupDataError("Release pointer escapes its source root") from None
    parts = _relative(target).parts
    _require(
        len(parts) == 2 and parts[0] == "releases" and SHA.fullmatch(parts[1]), "Invalid release"
    )
    release = _path(root, target)
    _directory(release)
    marker = _path(root, target + "/RELEASE_SHA")
    _file(marker)
    _require(marker.read_text().strip() == parts[1], "Release SHA does not match current")
    for relative in ("requirements.lock", "backend/app.py", "frontend/dist/index.html"):
        _file(_path(root, target + "/" + relative))
    return parts[1], target


def _catalog_selection(root):
    catalog = _path(root, "shared/catalog")
    _directory(catalog)
    pointer = catalog / "current"
    current, current_link = "shared/catalog", None
    snapshots = set()
    if pointer.exists() or pointer.is_symlink():
        _require(pointer.is_symlink(), "Catalog current must be a symlink")
        current_link = os.readlink(pointer)
        parts = _relative(current_link).parts
        _require(
            len(parts) == 3
            and parts[:2] == ("updates", "snapshots")
            and SNAPSHOT.fullmatch(parts[2]),
            "Invalid catalog snapshot pointer",
        )
        current += "/" + current_link
        snapshots.add(parts[2])
    for name in ("catalog.json", "catalog.sqlite"):
        _file(_path(root, current + "/" + name))
    status_path = _path(root, "shared/catalog/updates/status.json", missing=True)
    status = _json(status_path) if status_path.exists() else {}
    snapshot_path = _path(root, current + "/snapshot.json", missing=True)
    snapshot = _json(snapshot_path) if snapshot_path.exists() else {}
    previous = status.get("previous_snapshot", snapshot.get("parent"))
    if previous is not None:
        _require(
            isinstance(previous, str) and SNAPSHOT.fullmatch(previous), "Invalid prior snapshot"
        )
        snapshots.add(previous)
    for ident in snapshots:
        for name in ("catalog.json", "catalog.sqlite"):
            _file(_path(root, f"shared/catalog/updates/snapshots/{ident}/{name}"))
    return {
        "current": current,
        "current_link": current_link,
        "previous_snapshot": previous,
        "snapshots": sorted(snapshots),
        "history_scope": "current_and_previous_only",
    }


def _inventory(root, *, sync_files=False):
    entries = {}

    def walk(directory):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            if relative == "manifest.json":
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                entries[relative] = {"type": "symlink", "target": os.readlink(path)}
            elif stat.S_ISDIR(mode):
                entries[relative] = {"type": "directory"}
                walk(path)
            else:
                _file(path)
                entries[relative] = {
                    "type": "file",
                    "sha256": _hash(path, sync=sync_files),
                    "bytes": path.stat().st_size,
                    "mode": stat.S_IMODE(mode),
                }

    walk(root)
    return entries


def _empty_destination(destination, roots):
    destination = Path(destination).absolute()
    _require(not destination.is_symlink(), "Destination cannot be a symlink")
    destination = destination.resolve()
    for root in roots:
        _require(
            not destination.is_relative_to(root) and not root.is_relative_to(destination),
            "Backup destination overlaps its inputs",
        )
    if destination.exists():
        _directory(destination)
        _require(not any(destination.iterdir()), "Backup destination must be empty")
    else:
        destination.mkdir(parents=True, mode=0o700)
    destination.chmod(0o700)
    return destination


def capture_frozen(source_root, config_root, destination, *, system_root=Path("/")):
    """Copy frozen inputs; leave ordinary file fsyncs and validation to finalization."""
    source_root = Path(source_root).resolve(strict=True)
    config_root = Path(config_root).resolve(strict=True)
    system_root = Path(system_root).resolve(strict=True)
    destination = _empty_destination(destination, (source_root, config_root))
    _require(
        not (source_root / "deployment-pending.json").exists(),
        "Recover interrupted deployment before backing up",
    )
    release_sha, release_target = _code_release(source_root, allow_absolute=True)
    catalog = _catalog_selection(source_root)
    databases, absent = [], []
    for relative in DB_PATHS:
        source = _path(source_root, relative, missing=relative == DB_PATHS[2])
        if not source.exists():
            absent.append(relative)
            continue
        _backup_database(source, destination / relative)
        databases.append(relative)
    secret = _path(source_root, "shared/runtime/secrets.json", missing=True)
    if secret.exists():
        _copy_file(secret, destination / "shared/runtime/secrets.json")
    else:
        absent.append("shared/runtime/secrets.json")
    bilibili_session = _path(source_root, BILIBILI_SESSION_PATH, missing=True)
    if bilibili_session.exists():
        _copy_file(bilibili_session, destination / BILIBILI_SESSION_PATH)
    else:
        absent.append(BILIBILI_SESSION_PATH)
    _copy_tree(
        _path(source_root, "shared/catalog"),
        destination / "shared/catalog",
        excluded={"current", "updates", "remy-cache"},
    )
    status_relative = "shared/catalog/updates/status.json"
    status = _path(source_root, status_relative, missing=True)
    if status.exists():
        _copy_file(status, destination / status_relative)
    for ident in catalog["snapshots"]:
        relative = f"shared/catalog/updates/snapshots/{ident}"
        _copy_tree(_path(source_root, relative), destination / relative)
    if catalog["current_link"] is not None:
        (destination / "shared/catalog/current").symlink_to(catalog["current_link"])
    _copy_tree(_path(source_root, "shared/assets"), destination / "shared/assets")
    _copy_tree(
        _path(source_root, release_target), destination / release_target, excluded={"venv", ".venv"}
    )
    (destination / "current").symlink_to(release_target)
    for name in ("app.env", "deploy.env"):
        _copy_file(_path(config_root, name), destination / "config/mai-journal" / name)
    system_files = ["etc/caddy/Caddyfile", "etc/sudoers.d/mai-journal", *WAHLAP_PROXY_SYSTEM_PATHS]
    unit_root = _path(system_root, "etc/systemd/system", missing=True)
    if unit_root.exists():
        _directory(unit_root)
        system_files.extend(
            f"etc/systemd/system/{item.name}"
            for item in sorted(unit_root.iterdir())
            if UNIT.fullmatch(item.name)
        )
    for relative in system_files:
        source = _path(system_root, relative, missing=True)
        if source.exists():
            _copy_file(source, destination / "config/system" / relative)
        else:
            absent.append("config/system/" + relative)
    for directory in {
        catalog["current"],
        *(f"shared/catalog/updates/snapshots/{x}" for x in catalog["snapshots"]),
    }:
        relative = directory + "/catalog.sqlite"
        databases.append(relative)
    return {
        "schema_version": 3,
        "created_at": datetime.now(UTC).isoformat(),
        "release_sha": release_sha,
        "catalog": catalog,
        "databases": sorted(set(databases)),
        "optional_absent": sorted(absent),
    }


def finalize_capture(destination, metadata):
    """Create and verify the full manifest without reading the live installation."""
    destination = Path(destination).absolute()
    _directory(destination)
    destination = destination.resolve()
    manifest = {
        **metadata,
        "databases": {
            relative: _database_report(_path(destination, relative))
            for relative in metadata["databases"]
        },
        "entries": _inventory(destination, sync_files=True),
    }
    with (destination / "manifest.json").open("x", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return verify(destination)


def capture(source_root, config_root, destination, *, system_root=Path("/")):
    """Copy and validate in one call for callers without a service to resume."""
    metadata = capture_frozen(source_root, config_root, destination, system_root=system_root)
    return finalize_capture(destination, metadata)


def verify(destination):
    """Verify the exact file inventory, bounded links, databases and restore prerequisites."""
    destination = Path(destination).absolute()
    _directory(destination)
    destination = destination.resolve()
    manifest = _json(_path(destination, "manifest.json"))
    _require(
        set(manifest)
        == {
            "schema_version",
            "created_at",
            "release_sha",
            "catalog",
            "databases",
            "optional_absent",
            "entries",
        }
        and type(manifest.get("schema_version")) is int
        and manifest["schema_version"] in {1, 2, 3},
        "Unsupported backup manifest",
    )
    try:
        created_at = datetime.fromisoformat(manifest["created_at"])
    except (TypeError, ValueError):
        raise BackupDataError("Invalid backup timestamp") from None
    _require(created_at.utcoffset() is not None, "Backup timestamp must include timezone")
    entries = manifest.get("entries")
    _require(isinstance(entries, dict), "Missing backup inventory")
    for relative, entry in entries.items():
        _relative(relative)
        _require(isinstance(entry, dict), "Invalid backup inventory entry")
        if entry.get("type") == "symlink":
            _require(relative in {"current", "shared/catalog/current"}, "Unexpected backup symlink")
            _relative(entry.get("target"))
    # Inventory never follows links; exact equality rejects missing, altered and extra files.
    _require(_inventory(destination) == entries, "Backup inventory or checksum mismatch")
    optional_paths = LEGACY_OPTIONAL_PATHS
    if manifest["schema_version"] >= 2:
        optional_paths = OPTIONAL_PATHS
    if manifest["schema_version"] >= 3:
        optional_paths = (*optional_paths, BILIBILI_SESSION_PATH)
    _require(
        manifest["optional_absent"]
        == sorted(relative for relative in optional_paths if relative not in entries),
        "Optional input receipt mismatch",
    )
    if any(relative in entries for relative in WAHLAP_PROXY_PATHS):
        _require(
            all(relative in entries for relative in WAHLAP_PROXY_PATHS),
            "Wahlap proxy recovery files are incomplete",
        )
    sha, _ = _code_release(destination)
    _require(sha == manifest.get("release_sha"), "Backup release mismatch")
    catalog = _catalog_selection(destination)
    _require(catalog == manifest.get("catalog"), "Backup catalog selection mismatch")
    for relative in (*DB_PATHS[:2], *CONFIG_PATHS):
        _file(_path(destination, relative))
    _directory(_path(destination, "shared/assets"))
    expected_databases = set(DB_PATHS[:2])
    if (destination / DB_PATHS[2]).exists():
        expected_databases.add(DB_PATHS[2])
    expected_databases.add(catalog["current"] + "/catalog.sqlite")
    expected_databases.update(
        f"shared/catalog/updates/snapshots/{ident}/catalog.sqlite" for ident in catalog["snapshots"]
    )
    databases = manifest.get("databases")
    _require(
        isinstance(databases, dict) and set(databases) == expected_databases,
        "Missing backup database receipt",
    )
    for relative in expected_databases:
        _require(
            _database_report(_path(destination, relative)) == databases[relative],
            "Backup database receipt mismatch",
        )
    return manifest
