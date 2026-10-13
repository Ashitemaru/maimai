"""Synthetic wheel files for archive validation; these are never installed."""

import hashlib
import json

from deploy.deploy_release import PIP_VERSION, normalize_pin


def dependency_files(requirements="example==1.0.0\n"):
    files, entries = {}, []
    pins = [line for line in requirements.splitlines() if line and not line.startswith("#")]
    for pin in [*pins, f"pip=={PIP_VERSION}"]:
        name, version = pin.split("==")
        filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        payload = f"Synthetic wheel fixture: {pin}"
        files[f"dependencies/{filename}"] = payload
        entries.append(
            {
                "filename": filename,
                "requirement": normalize_pin(pin),
                "sha256": hashlib.sha256(payload.encode()).hexdigest(),
            }
        )
    files["dependencies/manifest.json"] = json.dumps(
        {
            "schema": 1,
            "target": {
                "implementation": "cpython",
                "python": "3.14",
                "system": "Linux",
                "machine": "x86_64",
                "glibc_min": "2.39",
            },
            "requirements_sha256": hashlib.sha256(requirements.encode()).hexdigest(),
            "wheels": entries,
        }
    )
    return files


def write_bundle(root):
    (root / "requirements.lock").write_text("example==1.0.0\n")
    for name, payload in dependency_files().items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
