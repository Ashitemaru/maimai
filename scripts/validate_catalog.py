#!/usr/bin/env python3
"""Validate an isolated catalog, including authority, hashes, retained rows and SQLite."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data" / "catalog"


def validate(catalog_dir: Path = ROOT) -> dict:
    try:
        from .catalog_chart_compiler import validate as validate_charts
    except ImportError:
        from catalog_chart_compiler import validate as validate_charts
    return validate_charts(catalog_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-dir", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(validate(args.catalog_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
