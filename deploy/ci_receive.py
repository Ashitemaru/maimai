#!/usr/bin/env python3
"""Store a release archive streamed by the CI deploy job for deploy_release.py.

Input on stdin: one line with the 40-hex commit SHA, then the archive bytes. The file
is placed where the deployer resumes downloads, named by its SHA-256. The deployer
still downloads SHA256SUMS from the GitHub release and uses this file only when the
digest matches; anything else is discarded and downloaded from GitHub instead.
"""

import hashlib
import os
import sys
import tempfile
from pathlib import Path

try:  # installed next to deploy_release.py in /opt/mai-journal
    from deploy_release import MAX_ARCHIVE, DeployError, valid_sha
except ImportError:  # repository checkout
    from deploy.deploy_release import MAX_ARCHIVE, DeployError, valid_sha

DOWNLOADS = Path("/srv/mai-journal/downloads")


def receive(source, downloads):
    try:
        sha = valid_sha(source.readline(64).decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise DeployError("Expected a commit SHA line before the archive") from exc
    name = f"mai-journal-{sha}.tar.gz"
    downloads.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(dir=downloads, prefix=".incoming-", delete=False) as out:
        try:
            while chunk := source.read(1 << 20):
                size += len(chunk)
                if size > MAX_ARCHIVE:
                    raise DeployError("Uploaded archive exceeded its size limit")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            os.unlink(out.name)
            raise
    for stale in downloads.glob(f"{name}*"):
        stale.unlink()
    target = downloads / f"{name}.{digest.hexdigest()}.part"
    os.replace(out.name, target)
    return target, size


def main():
    os.umask(0o077)
    try:
        target, size = receive(sys.stdin.buffer, DOWNLOADS)
    except (DeployError, OSError) as exc:
        print(f"Upload rejected: {exc}", file=sys.stderr)
        return 1
    print(f"Received {size} bytes as {target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
