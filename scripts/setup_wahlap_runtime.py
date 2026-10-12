#!/usr/bin/env python3
"""Download the pinned upstream into a private external runtime, never the source tree."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def install(runtime: Path, *, download_only=False):
    runtime = runtime.resolve()
    if runtime == REPO or REPO in runtime.parents:
        raise ValueError("Runtime must be outside the application repository")
    lock = json.loads((REPO / "deploy/wahlap-runtime.lock.json").read_text())
    runtime.mkdir(parents=True, exist_ok=True)
    base = f"https://raw.githubusercontent.com/XiaoLan9999/astrbot_plugin_maimai_updater/{lock['commit']}"
    for relative, digest in lock["files"].items():
        target = runtime / "upstream" / relative
        if target.is_symlink():
            raise ValueError("Runtime files must not be symlinks")
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            continue
        with urllib.request.urlopen(f"{base}/{relative}", timeout=30) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError(f"Upstream checksum mismatch: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".download")
        temporary.write_bytes(body)
        temporary.chmod(0o644)
        os.replace(temporary, target)
    if not download_only:
        python = runtime / ".venv/bin/python"
        if not python.exists():
            venv.EnvBuilder(with_pip=True).create(runtime / ".venv")
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *lock["dependencies"],
            ],
            check=True,
        )
    receipt = runtime / "runtime.json"
    receipt.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n")
    receipt.chmod(0o644)
    print(f"Pinned runtime prepared at {runtime}; no game account request was made.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    try:
        install(args.runtime_dir, download_only=args.download_only)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Runtime setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
