#!/usr/bin/env python3
"""Activate the release archive uploaded by the CI deploy job without overlapping workers."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

SHA = re.compile(r"[0-9a-f]{40}\Z")
SERVICE = "mai-journal.service"
READY_URL = "http://127.0.0.1:8766/api/ready"
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXTRACTED = 256 * 1024 * 1024
DEPENDENCY_SCHEMA = 1
PIP_VERSION = "26.2.1"
WHEEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl\Z")
CORE_SCRIPTS = {
    "build_catalog.py",
    "catalog_chart_compiler.py",
    "catalog_pipeline.py",
    "catalog_policy.py",
    "catalog_record_review.py",
    "catalog_versions.py",
    "download_jackets.py",
    "remy_html.py",
    "remy_release.py",
    "validate_catalog.py",
    "__init__.py",
}


class DeployError(Exception):
    """An actionable deployment failure with no credential-bearing response body."""


def log(message):
    print(f"{datetime.now(UTC).isoformat()} {message}", flush=True)


def valid_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise DeployError("Invalid release SHA")
    return value


def allowed_file(parts):
    if parts[0] == "dependencies":
        return len(parts) == 2 and (
            parts[1] == "manifest.json" or WHEEL_NAME.fullmatch(parts[1]) is not None
        )
    if parts == ("deploy", "wahlap-runtime.lock.json"):
        return True
    if len(parts) == 1:
        return parts[0] in {"RELEASE_SHA", "requirements.lock"}
    if parts[0] == "backend":
        return len(parts) == 2 and parts[1].endswith(".py") and not parts[1].startswith("test")
    if parts[0] == "scripts":
        return len(parts) == 2 and parts[1] in CORE_SCRIPTS
    if parts[:2] == ("frontend", "dist"):
        return len(parts) > 2
    return (
        parts[0] == "deploy"
        and all(not p.startswith(".") for p in parts)
        and PurePosixPath(parts[-1]).suffix
        in {".py", ".service", ".timer", ".sudoers", ".example", ".md", ".sh", ".txt"}
    )


def extract_archive(archive, destination, sha, wheel_cache=None):
    """Stream only regular release files; never extract links, devices or private data.

    The CI deploy job omits wheels the server already has; they are restored from
    `wheel_cache` by digest and then verified with the rest of the bundle.
    """
    seen, total = set(), 0
    try:
        with tarfile.open(archive, "r|gz") as tar:
            for member in tar:
                if "\\" in member.name or ".." in member.name.split("/"):
                    raise DeployError("Unsafe archive member path")
                relative = PurePosixPath(member.name)
                if relative.is_absolute():
                    raise DeployError("Absolute archive member path")
                parts = relative.parts
                if not parts and member.isdir():
                    continue
                if not parts or relative.as_posix() in seen:
                    raise DeployError("Duplicate or empty archive member")
                seen.add(relative.as_posix())
                if len(seen) > 10000:
                    raise DeployError("Too many archive members")
                if member.isdir():
                    if parts[0] not in {"backend", "scripts", "frontend", "deploy", "dependencies"}:
                        raise DeployError("Archive contains a non-code directory")
                    if parts[0] == "frontend" and len(parts) > 1 and parts[1] != "dist":
                        raise DeployError("Only built frontend assets belong in a release")
                    (destination / relative).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile() or not allowed_file(parts):
                    raise DeployError("Archive contains links, special files or non-release data")
                total += member.size
                if member.size < 0 or total > MAX_EXTRACTED:
                    raise DeployError("Unpacked release exceeded its size limit")
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise DeployError("Cannot read archive member")
                with source, target.open("xb") as handle:
                    shutil.copyfileobj(source, handle, length=1024 * 1024)
                target.chmod(0o600)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise DeployError("Invalid release archive") from exc
    for required in (
        "backend/app.py",
        "backend/store.py",
        "scripts/catalog_pipeline.py",
        "frontend/dist/index.html",
        "requirements.lock",
        "RELEASE_SHA",
    ):
        if not (destination / required).is_file():
            raise DeployError(f"Missing required release file: {required}")
    if (destination / "RELEASE_SHA").read_text().strip() != sha:
        raise DeployError("Archive RELEASE_SHA does not match the approved commit")
    requirement_pins(destination / "requirements.lock")
    if wheel_cache is not None:
        restore_cached_wheels(destination, wheel_cache)
    dependency_bundle(destination)


def manifest_wheels(release):
    try:
        manifest = json.loads((release / "dependencies/manifest.json").read_text())
        return [(w["filename"], w["sha256"]) for w in manifest["wheels"]]
    except (OSError, ValueError, UnicodeError, KeyError, TypeError) as exc:
        raise DeployError("Invalid dependency bundle manifest") from exc


def restore_cached_wheels(release, cache):
    for name, digest in manifest_wheels(release):
        target = release / "dependencies" / name
        cached = cache / f"{digest}.whl"
        if WHEEL_NAME.fullmatch(name) and not target.exists() and cached.is_file():
            shutil.copyfile(cached, target)
            target.chmod(0o600)


def cache_wheels(release, cache):
    """Keep exactly the verified wheels of `release` for the next upload."""
    cache.mkdir(mode=0o700, exist_ok=True)
    keep = set()
    for name, digest in manifest_wheels(release):
        keep.add(f"{digest}.whl")
        cached = cache / f"{digest}.whl"
        if not cached.exists():
            temporary = cache / f".{digest}-{uuid.uuid4().hex}"
            shutil.copyfile(release / "dependencies" / name, temporary)
            temporary.replace(cached)
    for path in cache.iterdir():
        if path.name not in keep:
            path.unlink()


def requirement_pins(path):
    lines = path.read_text().splitlines()
    pins = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    if not pins or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9.!+_-]*", pin)
        for pin in pins
    ):
        raise DeployError(
            "Requirements must contain version pins only, without URLs or pip options"
        )
    normalized = {normalize_pin(pin) for pin in pins}
    if len({pin.split("==")[0] for pin in normalized}) != len(pins):
        raise DeployError("Requirements must not contain duplicate packages")
    return normalized


def normalize_pin(pin):
    name, version = pin.split("==")
    return re.sub(r"[-_.]+", "-", name).lower() + "==" + version


def dependency_bundle(release, *, check_host=False):
    """Validate all vendored wheels before creating a venv or stopping the service."""
    folder = release / "dependencies"
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise DeployError("Release has no offline dependency bundle; rebuild it with current CI")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise DeployError("Invalid dependency bundle manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != DEPENDENCY_SCHEMA:
        raise DeployError("Unsupported dependency bundle schema")
    target = manifest.get("target")
    if not isinstance(target, dict) or (
        target.get("implementation") != "cpython"
        or target.get("python") != "3.14"
        or target.get("system") != "Linux"
        or target.get("machine") != "x86_64"
        or not isinstance(target.get("glibc_min"), str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+", target["glibc_min"])
    ):
        raise DeployError("Unsupported dependency bundle target")
    if check_host:
        libc, version = platform.libc_ver()
        compatible = (
            sys.implementation.name == target["implementation"]
            and f"{sys.version_info.major}.{sys.version_info.minor}" == target["python"]
            and platform.system() == target["system"]
            and platform.machine() == target["machine"]
            and libc == "glibc"
            and re.fullmatch(r"[0-9]+\.[0-9]+", version)
            and tuple(map(int, version.split(".")))
            >= tuple(map(int, target["glibc_min"].split(".")))
        )
        if not compatible:
            raise DeployError(
                "Dependency bundle requires CPython 3.14, Linux x86_64 and glibc >= "
                + target["glibc_min"]
            )
    requirements = release / "requirements.lock"
    if manifest.get("requirements_sha256") != hashlib.sha256(requirements.read_bytes()).hexdigest():
        raise DeployError("Dependency bundle does not match requirements.lock")
    pins = requirement_pins(requirements) | {f"pip=={PIP_VERSION}"}
    wheels = manifest.get("wheels")
    if not isinstance(wheels, list) or len(wheels) != len(pins):
        raise DeployError("Dependency bundle must contain exactly one wheel for every pin")
    files, found, locks = {"manifest.json"}, set(), {}
    for wheel in wheels:
        if not isinstance(wheel, dict):
            raise DeployError("Invalid dependency wheel entry")
        name, pin, digest = (wheel.get(key) for key in ("filename", "requirement", "sha256"))
        if (
            not isinstance(name, str)
            or not WHEEL_NAME.fullmatch(name)
            or name in files
            or not isinstance(pin, str)
            or pin not in pins
            or pin in found
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise DeployError("Invalid or duplicate dependency wheel")
        parts = name[:-4].split("-")
        if len(parts) not in {5, 6} or normalize_pin(f"{parts[0]}=={parts[1]}") != pin:
            raise DeployError("Dependency wheel filename does not match its package pin")
        path = folder / name
        if not path.is_file() or path.is_symlink():
            raise DeployError("Missing or unsafe dependency wheel")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise DeployError("Dependency wheel checksum mismatch")
        files.add(name)
        found.add(pin)
        locks[pin] = f"{pin} --hash=sha256:{digest}\n"
    if {path.name for path in folder.iterdir()} != files or found != pins:
        raise DeployError("Dependency bundle contains missing or unexpected files")
    return locks


def fsync_directory(path):
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def atomic_json(path, value):
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    with temporary.open("x") as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    fsync_directory(path.parent)


def switch_current(root, sha):
    current = root / "current"
    if sha is None:
        current.unlink(missing_ok=True)
    else:
        temporary = root / f".current-{uuid.uuid4().hex}"
        temporary.symlink_to(Path("releases") / valid_sha(sha))
        temporary.replace(current)
    fsync_directory(root)


class Host:
    def run(self, command, *, timeout=900, cwd=None):
        try:
            subprocess.run(command, cwd=cwd, check=True, timeout=timeout, capture_output=True)
        except (subprocess.SubprocessError, OSError) as exc:
            # Do not echo arbitrary command output, pip configuration, or environment values.
            raise DeployError(
                "Server command failed; inspect service status or rerun preparation"
            ) from exc

    def install(self, release):
        locks = dependency_bundle(release, check_host=True)
        bootstrap = release / "install-pip.txt"
        requirements = release / "install-requirements.txt"
        bootstrap.write_text(locks.pop(f"pip=={PIP_VERSION}"))
        requirements.write_text("".join(locks[pin] for pin in sorted(locks)))
        self.run([sys.executable, "-m", "venv", str(release / "venv")])
        python = str(release / "venv/bin/python")
        pip = [python, "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-cache-dir"]
        install = [
            "install",
            "--no-index",
            "--find-links",
            str(release / "dependencies"),
            "--only-binary=:all:",
            "--require-hashes",
            "--no-deps",
        ]
        self.run([*pip, *install, "--upgrade", "--requirement", str(bootstrap)])
        self.run([*pip, *install, "--requirement", str(requirements)])
        self.run([*pip, "check"])

    def service(self, action):
        if action not in {"start", "stop", "restart"}:
            raise DeployError("Unsupported service operation")
        self.run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", action, SERVICE], timeout=225)

    def ready(self, sha, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(READY_URL, timeout=3) as response:
                    data = json.loads(response.read(16385))
                if data.get("ok") is True and data.get("release_sha") == sha:
                    return True
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1)
        return False


class Deployer:
    def __init__(self, root, host=None):
        self.root = Path(root).resolve()
        self.releases = self.root / "releases"
        self.incoming = self.root / "incoming"
        self.wheels = self.root / "wheels"
        self.journal = self.root / "deployment-pending.json"
        self.host = host or Host()

    def uploaded(self):
        """The SHA of the single archive left by ci_receive.py, if any."""
        if not self.incoming.is_dir() or self.incoming.is_symlink():
            return None
        found = [
            path
            for path in self.incoming.iterdir()
            if re.fullmatch(r"mai-journal-[0-9a-f]{40}\.tar\.gz", path.name)
        ]
        if len(found) != 1 or found[0].is_symlink() or not found[0].is_file():
            return None
        if found[0].stat().st_size > MAX_ARCHIVE:
            raise DeployError("Uploaded archive exceeded its size limit")
        return found[0].name.removeprefix("mai-journal-").removesuffix(".tar.gz")

    def installed(self):
        current = self.root / "current"
        if not current.is_symlink():
            if current.exists():
                raise DeployError("current must be a release symlink")
            return None
        resolved = current.resolve()
        if resolved.parent != self.releases or not resolved.is_dir():
            raise DeployError("current points outside the release directory or is broken")
        sha = valid_sha(resolved.name)
        if (resolved / "RELEASE_SHA").read_text().strip() != sha:
            raise DeployError("Installed release marker does not match current")
        return sha

    def clear_journal(self):
        self.journal.unlink(missing_ok=True)
        fsync_directory(self.root)

    def rollback(self, previous, target):
        current = self.installed()
        if current not in (previous, target):
            raise DeployError("Deployment pointer changed outside the active transaction")
        self.host.service("stop")
        switch_current(self.root, previous)
        if previous:
            self.host.service("start")
            if not self.host.ready(previous):
                raise DeployError("Rollback release did not become ready; manual recovery required")
        self.clear_journal()
        log("Previous code restored; personal databases were not restored or overwritten")

    def recover(self):
        if not self.journal.exists():
            return
        try:
            state = json.loads(self.journal.read_text())
            previous = valid_sha(state["previous"]) if state["previous"] else None
            target = valid_sha(state["target"])
        except (ValueError, KeyError, TypeError) as exc:
            raise DeployError("Invalid deployment journal; manual recovery required") from exc
        if previous and not (self.releases / previous / "RELEASE_SHA").is_file():
            raise DeployError("Previous release missing; manual recovery required")
        log("Recovering an interrupted activation before installing new code")
        self.rollback(previous, target)

    def backup(self, previous, target):
        label = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        directory = self.root / "backups" / label
        directory.mkdir(parents=True, mode=0o700)
        names = {
            "personal.sqlite3": self.root / "shared/runtime/personal.sqlite3",
            "adjudications.sqlite3": self.root / "shared/catalog/updates/adjudications.sqlite3",
            "auth.sqlite3": self.root / "shared/runtime/auth.sqlite3",
        }
        completed = []
        for name, source in names.items():
            if not source.is_file():
                if name == "personal.sqlite3":
                    raise DeployError("Personal database must be initialized before deployment")
                continue
            with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
                with contextlib.closing(sqlite3.connect(directory / name)) as dest:
                    src.backup(dest)
                    if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise DeployError("SQLite backup integrity check failed")
            (directory / name).chmod(0o600)
            completed.append(name)
        atomic_json(
            directory / "receipt.json",
            {"previous": previous, "target": target, "databases": completed},
        )
        return directory

    def prepare(self, sha):
        release = self.releases / sha
        if release.exists():
            if release.is_symlink() or not release.is_dir():
                raise DeployError("Unexpected release path")
            # Only inactive candidates reach preparation. A previous interrupted
            # venv install may be incomplete; rebuild instead of trusting it.
            shutil.rmtree(release)
        archive = self.incoming / f"mai-journal-{sha}.tar.gz"
        try:
            with tempfile.TemporaryDirectory(prefix=".prepare-", dir=self.releases) as temp:
                extracted = Path(temp) / "code"
                extracted.mkdir()
                extract_archive(archive, extracted, sha, self.wheels)
                extracted.replace(release)
        finally:
            # A rerun of the CI deploy job uploads again; never reuse a consumed archive.
            archive.unlink(missing_ok=True)
        try:
            self.host.install(release)
            (release / "release.env").write_text(f"MAIMAI_RELEASE_SHA={sha}\n")
        except Exception:
            shutil.rmtree(release)
            raise
        cache_wheels(release, self.wheels)
        return release

    def run_locked(self):
        self.recover()
        previous = self.installed()
        sha = self.uploaded()
        if sha is None:
            log("No uploaded release archive; run the CI deploy job")
            return "pending"
        if previous == sha:
            (self.incoming / f"mai-journal-{sha}.tar.gz").unlink()
            log(f"Already installed {sha}; no changes")
            return "unchanged"
        log(f"Preparing uploaded release {sha}")
        self.prepare(sha)
        atomic_json(self.journal, {"previous": previous, "target": sha})
        try:
            self.host.service("stop")
            self.backup(previous, sha)
            switch_current(self.root, sha)
            self.host.service("start")
            if not self.host.ready(sha):
                raise DeployError("New release did not become ready")
            self.clear_journal()
            log(f"Activated {sha}")
            return "deployed"
        except Exception:
            self.rollback(previous, sha)
            raise

    def run(self):
        self.releases.mkdir(parents=True, exist_ok=True)
        with (self.root / "deploy.lock").open("a") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log("Another deployment is running; skipping this trigger")
                return "busy"
            return self.run_locked()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/srv/mai-journal"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = Deployer(args.root).run()
    except (DeployError, OSError, sqlite3.Error, ValueError) as exc:
        log(f"Deployment failed: {type(exc).__name__}: {exc}")
        return 1
    # The CI deploy job reports this exit status; nothing deployed must not look green.
    return 1 if result in ("pending", "busy") else 0


if __name__ == "__main__":
    raise SystemExit(main())
