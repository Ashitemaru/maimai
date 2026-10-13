"""Download and test the production wheel bundle on the matching Linux CI runner."""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

from deploy.deploy_release import (
    DEPENDENCY_SCHEMA,
    PIP_VERSION,
    DeployError,
    Host,
    dependency_bundle,
    normalize_pin,
    requirement_pins,
)

ROOT = Path(__file__).resolve().parents[1]


def create_manifest(requirements: Path, wheels: Path, target: dict) -> dict:
    pins = requirement_pins(requirements) | {f"pip=={PIP_VERSION}"}
    entries = []
    for path in sorted(wheels.iterdir()):
        if path.suffix != ".whl" or path.is_symlink() or not path.is_file():
            raise DeployError("Dependency download must contain regular wheels only")
        parts = path.name[:-4].split("-")
        if len(parts) not in {5, 6}:
            raise DeployError("Invalid wheel filename")
        pin = normalize_pin(f"{parts[0]}=={parts[1]}")
        if pin not in pins:
            raise DeployError("Downloaded wheel is not pinned in requirements.lock")
        entries.append(
            {
                "filename": path.name,
                "requirement": pin,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {
        "schema": DEPENDENCY_SCHEMA,
        "target": target,
        "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
        "wheels": entries,
    }


def build(requirements: Path, destination: Path) -> None:
    libc, version = platform.libc_ver()
    if (
        sys.implementation.name != "cpython"
        or sys.version_info[:2] != (3, 14)
        or platform.system() != "Linux"
        or platform.machine() != "x86_64"
        or libc != "glibc"
    ):
        raise DeployError("Build production dependencies on CPython 3.14 Linux x86_64 with glibc")
    if destination.exists():
        raise DeployError("Dependency output already exists; use a clean build directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-wheels-", dir=destination.parent) as temporary:
        release = Path(temporary)
        (release / "requirements.lock").write_bytes(requirements.read_bytes())
        wheels = release / "dependencies"
        wheels.mkdir()
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "download",
                "--index-url",
                "https://pypi.org/simple",
                "--only-binary=:all:",
                "--no-deps",
                "--dest",
                str(wheels),
                "--requirement",
                str(requirements),
                f"pip=={PIP_VERSION}",
            ],
            check=True,
            timeout=600,
        )
        target = {
            "implementation": "cpython",
            "python": "3.14",
            "system": "Linux",
            "machine": "x86_64",
            "glibc_min": version,
        }
        manifest = create_manifest(requirements, wheels, target)
        (wheels / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        dependency_bundle(release, check_host=True)
        Host().install(release)
        subprocess.run(
            [
                str(release / "venv/bin/python"),
                "-c",
                "import fastapi, httpx, PIL.Image, pydantic, pydantic_core, uvicorn; "
                "PIL.Image.new('RGB', (2, 2)).resize((1, 1)); print('Offline runtime imports passed')",
            ],
            check=True,
            timeout=30,
        )
        wheels.replace(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "release-dependencies")
    args = parser.parse_args()
    build(ROOT / "requirements.lock", args.destination.resolve())


if __name__ == "__main__":
    main()
