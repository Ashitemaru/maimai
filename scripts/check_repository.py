"""Reject private runtime artifacts and obvious credentials in tracked source files."""

import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOTS = (
    "data/",
    "output/",
    "backend/runtime/",
    ".playwright-mcp/",
    "ashitemaru-bilibili-covers/",
)
PRIVATE_EXTENSIONS = {".sqlite3", ".db", ".zip", ".pem", ".key", ".otf", ".ttf"}
CREDENTIAL_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
)


def main() -> int:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    issues = []
    for raw_path in filter(None, tracked):
        name = raw_path.decode("utf-8")
        path = PurePosixPath(name)
        if (
            name.startswith(PRIVATE_ROOTS)
            or path.suffix in PRIVATE_EXTENSIONS
            or "node_modules" in path.parts
            or "__pycache__" in path.parts
            or path.name == "secrets.json"
            or (path.name.startswith(".env") and path.name != ".env.example")
        ):
            issues.append(f"{name}: private/generated artifact must not be tracked")
            continue
        # Inspect the index, matching the exact content that a commit will publish.
        payload = subprocess.check_output(["git", "show", f":{name}"], cwd=ROOT)
        if len(payload) > 1024 * 1024:
            issues.append(f"{name}: exceeds the 1 MiB source-file limit")
        if any(pattern.search(payload) for pattern in CREDENTIAL_PATTERNS):
            issues.append(f"{name}: credential-shaped content; inspect locally")
    if issues:
        print("\n".join(issues))
        return 1
    print("Repository policy passed: no private artifacts or recognized credentials in the index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
