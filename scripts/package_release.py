"""Build a reproducible code-only release; runtime data never enters the archive."""

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
REQUIRED_FILES = (
    "frontend/dist/index.html",
    "backend/app.py",
    "backend/store.py",
    "scripts/catalog_pipeline.py",
    "requirements.lock",
)


def is_release_source(path: Path) -> bool:
    if path.parts[0] == "backend":
        return len(path.parts) == 2 and path.suffix == ".py" and not path.name.startswith("test")
    if path.parts[0] == "scripts":
        return len(path.parts) == 2 and path.name in CORE_SCRIPTS
    return (
        path.parts[0] == "deploy"
        and path.suffix == ".py"
        and all(not part.startswith(".") for part in path.parts)
    )


def release_paths(root: Path) -> list[Path]:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    selected = [root / "requirements.lock"]
    for name in filter(None, tracked):
        path = Path(name)
        if is_release_source(path):
            selected.append(root / path)
    selected.extend(path for path in (root / "frontend/dist").rglob("*") if path.is_file())
    for required in REQUIRED_FILES:
        if root / required not in selected:
            raise ValueError(f"Required release file missing: {required}")
    for path in selected:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Release input must be a regular file: {path.relative_to(root)}")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Release file resolves outside the source tree")
    return sorted(set(selected))


def package(root: Path, destination: Path, sha: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Release SHA must identify a full commit")
    paths = release_paths(root)
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"mai-journal-{sha}.tar.gz"
    with archive.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as tar:
                entries = [(str(path.relative_to(root)), path.read_bytes()) for path in paths]
                entries.append(("RELEASE_SHA", (sha + "\n").encode()))
                for name, payload in entries:
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(payload))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n")
    return archive


def run_gh(*args: str, allow_missing: bool = False) -> str | None:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        if allow_missing and "HTTP 404" in result.stderr:
            return None
        raise RuntimeError("GitHub release command failed; existing assets were not overwritten")
    return result.stdout


def publish_release(destination: Path, sha: str, repository: str, *, gh=run_gh) -> None:
    """Permit identical reruns, but never replace an asset under an existing commit tag."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Release SHA must identify a full commit")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or any(
        part in {".", ".."} for part in repository.split("/")
    ):
        raise ValueError("Release repository must be owner/name")
    tag = f"app-{sha}"
    names = (f"mai-journal-{sha}.tar.gz", "SHA256SUMS")
    expected = {name: (destination / name).read_bytes() for name in names}
    endpoint = f"repos/{repository}/releases/tags/{tag}"
    metadata = gh("api", endpoint, allow_missing=True)
    if metadata is None:
        gh(
            "release",
            "create",
            tag,
            *(str(destination / name) for name in names),
            "--repo",
            repository,
            "--target",
            sha,
            "--title",
            f"mai journal {sha[:12]}",
            "--notes",
            "Code-only release built after every required CI job passed. "
            "Personal data and credentials are not included.",
            "--latest",
        )
        metadata = gh("api", endpoint)
    release = json.loads(metadata)
    if release.get("tag_name") != tag or release.get("draft") or release.get("prerelease"):
        raise RuntimeError("Existing release metadata differs; it was not changed")
    commit = json.loads(gh("api", f"repos/{repository}/commits/{tag}"))
    if commit.get("sha") != sha:
        raise RuntimeError("Existing release tag points to another commit; it was not changed")
    for name in names:
        assets = [asset for asset in release.get("assets", []) if asset.get("name") == name]
        if len(assets) != 1 or assets[0].get("state") != "uploaded":
            raise RuntimeError("Existing release assets are incomplete; they were not changed")
    with tempfile.TemporaryDirectory(prefix="verify-release-") as folder:
        gh(
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--dir",
            folder,
            "--pattern",
            names[0],
            "--pattern",
            names[1],
        )
        for name, payload in expected.items():
            if (Path(folder) / name).read_bytes() != payload:
                raise RuntimeError(f"Existing release asset differs: {name}; it was not changed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish", metavar="OWNER/REPO", help="Publish or verify the exact release"
    )
    args = parser.parse_args()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    print(package(ROOT, ROOT / "release", commit))
    if args.publish:
        publish_release(ROOT / "release", commit, args.publish)
