#!/usr/bin/env python3
"""Pull a successful public-main release and activate it without overlapping workers."""

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
from urllib.parse import urlencode, urlsplit

SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
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
DOWNLOAD_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


class DeployError(Exception):
    """An actionable deployment failure with no credential-bearing response body."""


def log(message):
    print(f"{datetime.now(UTC).isoformat()} {message}", flush=True)


def valid_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise DeployError("Invalid release SHA")
    return value


def https_url(url, hosts):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise DeployError("Refused an unofficial or non-HTTPS download URL")
    return url


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        https_url(newurl, DOWNLOAD_HOSTS)
        return super().redirect_request(request, fp, code, message, headers, newurl)


class GitHub:
    def __init__(self, repository):
        if not REPOSITORY.fullmatch(repository) or any(
            p in {".", ".."} for p in repository.split("/")
        ):
            raise DeployError("Set MAIMAI_REPOSITORY to a public owner/repository")
        self.repository = repository
        self.base = f"https://api.github.com/repos/{repository}"
        self.opener = urllib.request.build_opener(OfficialRedirects())

    def read(self, url, limit):
        hosts = {"api.github.com"} if url.startswith(self.base + "/") else DOWNLOAD_HOSTS
        https_url(url, hosts)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "mai-journal-pull-deploy/1",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )
        try:
            with self.opener.open(request, timeout=45) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise DeployError(f"GitHub HTTP {exc.code}; current service was not changed") from None
        except (OSError, urllib.error.URLError) as exc:
            raise DeployError("GitHub request failed; retry on the next timer run") from exc
        if len(data) > limit:
            raise DeployError("Download exceeded its size limit")
        return data

    def api(self, suffix):
        content = self.read(self.base + suffix, 4 * 1024 * 1024)
        if content is None:
            return None
        try:
            return json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DeployError("GitHub returned invalid JSON") from exc

    def main_sha(self):
        result = self.api("/commits/main")
        if not isinstance(result, dict):
            raise DeployError("Public main branch was not found")
        return valid_sha(result.get("sha"))

    def release(self, sha):
        query = urlencode({"branch": "main", "event": "push", "head_sha": sha, "per_page": 100})
        runs = self.api("/actions/workflows/ci.yml/runs?" + query) or {}
        accepted = any(
            run.get("head_sha") == sha
            and run.get("head_branch") == "main"
            and run.get("event") == "push"
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("repository", {}).get("full_name", "").lower() == self.repository.lower()
            and run.get("head_repository", {}).get("full_name", "").lower()
            == self.repository.lower()
            for run in runs.get("workflow_runs", [])
        )
        if not accepted:
            return None
        tag = f"app-{sha}"
        release = self.api(f"/releases/tags/{tag}")
        if not release:
            return None
        if release.get("tag_name") != tag or release.get("draft") or release.get("prerelease"):
            raise DeployError("Release metadata does not match the approved SHA")
        tagged = self.api(f"/commits/{tag}")
        if not tagged or tagged.get("sha") != sha:
            raise DeployError("Release tag does not resolve to the approved commit")
        names = [f"mai-journal-{sha}.tar.gz", "SHA256SUMS"]
        assets = {}
        for name in names:
            matches = [asset for asset in release.get("assets", []) if asset.get("name") == name]
            if len(matches) != 1 or matches[0].get("state") != "uploaded":
                raise DeployError("Release is missing exactly one required uploaded asset")
            url = matches[0].get("browser_download_url", "")
            expected = f"https://github.com/{self.repository}/releases/download/{tag}/{name}"
            if url != expected:
                raise DeployError("Release asset URL does not match its repository and tag")
            assets[name] = url
        return assets

    def download(self, assets, sha, destination):
        name = f"mai-journal-{sha}.tar.gz"
        checksums = self.read(assets["SHA256SUMS"], 65536)
        data = self.read(assets[name], MAX_ARCHIVE)
        if checksums is None or data is None:
            raise DeployError("A release asset disappeared before download")
        expected = checksum_for(checksums, name)
        if hashlib.sha256(data).hexdigest() != expected:
            raise DeployError("Release archive checksum mismatch")
        destination.write_bytes(data)


def checksum_for(data, name):
    try:
        rows = [line.split() for line in data.decode("ascii").splitlines() if line.strip()]
    except UnicodeDecodeError as exc:
        raise DeployError("Invalid checksum manifest") from exc
    values = [parts[0] for parts in rows if len(parts) == 2 and parts[1].lstrip("*") == name]
    if len(values) != 1 or not re.fullmatch(r"[0-9a-f]{64}", values[0]):
        raise DeployError("Checksum manifest must contain exactly one matching archive")
    return values[0]


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


def extract_archive(archive, destination, sha):
    """Stream only regular release files; never extract links, devices or private data."""
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
    dependency_bundle(destination)


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
    def __init__(self, root, github, host=None, *, retry_failed=False):
        self.root = Path(root).resolve()
        self.releases = self.root / "releases"
        self.journal = self.root / "deployment-pending.json"
        self.failed_release = self.root / "failed-release.json"
        self.github = github
        self.host = host or Host()
        self.retry_failed = retry_failed

    def failed_sha(self):
        if not self.failed_release.exists():
            return None
        try:
            return valid_sha(json.loads(self.failed_release.read_text())["sha"])
        except (ValueError, KeyError, TypeError) as exc:
            raise DeployError("Invalid failed-release marker; inspect before retrying") from exc

    def mark_failed(self, sha):
        atomic_json(self.failed_release, {"sha": sha, "failed_at": datetime.now(UTC).isoformat()})

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
        log("Recovering an interrupted activation before checking for new code")
        try:
            self.mark_failed(target)
        finally:
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

    def prepare(self, sha, assets):
        release = self.releases / sha
        if release.exists():
            if release.is_symlink() or not release.is_dir():
                raise DeployError("Unexpected release path")
            # Only inactive candidates reach preparation. A previous interrupted
            # venv install may be incomplete; rebuild instead of trusting it.
            shutil.rmtree(release)
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=self.releases) as temp:
            staging = Path(temp)
            archive = staging / "release.tar.gz"
            self.github.download(assets, sha, archive)
            extracted = staging / "code"
            extracted.mkdir()
            extract_archive(archive, extracted, sha)
            extracted.replace(release)
        try:
            self.host.install(release)
            (release / "release.env").write_text(f"MAIMAI_RELEASE_SHA={sha}\n")
        except Exception:
            shutil.rmtree(release)
            raise
        return release

    def run_locked(self):
        self.recover()
        previous = self.installed()
        sha = self.github.main_sha()
        if previous == sha:
            log(f"Already installed {sha}; no changes")
            return "unchanged"
        if self.failed_sha() == sha and not self.retry_failed:
            log(f"Skipping failed release {sha}; use --retry-failed after fixing the cause")
            return "rejected"
        assets = self.github.release(sha)
        if assets is None:
            log(f"Waiting for successful main CI and exact release {sha}")
            return "pending"
        log(f"Preparing approved release {sha}")
        self.prepare(sha, assets)
        if self.github.main_sha() != sha:
            log("main advanced during preparation; current service was not changed")
            return "superseded"
        atomic_json(self.journal, {"previous": previous, "target": sha})
        try:
            self.host.service("stop")
            self.backup(previous, sha)
            switch_current(self.root, sha)
            self.host.service("start")
            if not self.host.ready(sha):
                raise DeployError("New release did not become ready")
            self.clear_journal()
            self.failed_release.unlink(missing_ok=True)
            fsync_directory(self.root)
            log(f"Activated {sha}")
            return "deployed"
        except Exception:
            try:
                self.mark_failed(sha)
            finally:
                self.rollback(previous, sha)
            raise

    def run(self):
        self.releases.mkdir(parents=True, exist_ok=True)
        with (self.root / "deploy.lock").open("a") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log("Another deployment is running; skipping this timer tick")
                return "busy"
            return self.run_locked()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("MAIMAI_REPOSITORY", ""))
    parser.add_argument("--root", type=Path, default=Path("/srv/mai-journal"))
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Explicitly retry the current failed SHA after fixing its cause",
    )
    args = parser.parse_args()
    os.umask(0o077)
    try:
        Deployer(args.root, GitHub(args.repository), retry_failed=args.retry_failed).run()
    except (DeployError, OSError, sqlite3.Error, ValueError) as exc:
        log(f"Deployment failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
