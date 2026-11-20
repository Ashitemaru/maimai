#!/usr/bin/env python3
"""Server side of the CI deploy connection, run as the service user by ci-deploy.sh.

`list` prints the digests of cached dependency wheels, so CI can leave them out of
the upload. `upload` reads one line with the 40-hex commit SHA, then the release
archive, and stores it as the single pending archive for deploy_release.py.
"""

import os
import sys
import tempfile
from pathlib import Path

try:  # installed next to deploy_release.py in /opt/mai-journal
    from deploy_release import MAX_ARCHIVE, DeployError, valid_sha
except ImportError:  # repository checkout
    from deploy.deploy_release import MAX_ARCHIVE, DeployError, valid_sha

ROOT = Path("/srv/mai-journal")


def cached_digests(root):
    wheels = root / "wheels"
    if not wheels.is_dir():
        return []
    return sorted(p.name.removesuffix(".whl") for p in wheels.glob("*.whl"))


def receive(source, root):
    try:
        sha = valid_sha(source.readline(64).decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise DeployError("Expected a commit SHA line before the archive") from exc
    incoming = root / "incoming"
    incoming.mkdir(mode=0o700, exist_ok=True)
    size = 0
    with tempfile.NamedTemporaryFile(dir=incoming, prefix=".upload-", delete=False) as out:
        try:
            while chunk := source.read(1 << 20):
                size += len(chunk)
                if size > MAX_ARCHIVE:
                    raise DeployError("Uploaded archive exceeded its size limit")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            os.unlink(out.name)
            raise
    for stale in incoming.glob("mai-journal-*"):
        stale.unlink()
    target = incoming / f"mai-journal-{sha}.tar.gz"
    os.replace(out.name, target)
    return target, size


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    os.umask(0o077)
    if args == ["list"]:
        print("\n".join(cached_digests(ROOT)))
        return 0
    if args != ["upload"]:
        print("Usage: ci_receive.py list|upload", file=sys.stderr)
        return 2
    try:
        target, size = receive(sys.stdin.buffer, ROOT)
    except (DeployError, OSError) as exc:
        print(f"Upload rejected: {exc}", file=sys.stderr)
        return 1
    print(f"Received {size} bytes as {target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
