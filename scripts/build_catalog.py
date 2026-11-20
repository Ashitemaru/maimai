#!/usr/bin/env python3
"""Build a deterministic offline catalog from saved API responses (no network).

Build:  python3 scripts/build_catalog.py
Search: python3 scripts/build_catalog.py --search 'Secret Sleuth'
        python3 scripts/build_catalog.py --id 10030
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "data" / "catalog"
JP_VERSION_BOUNDARIES = [
    (10000, "maimai"),
    (11000, "maimai PLUS"),
    (12000, "GreeN"),
    (13000, "GreeN PLUS"),
    (14000, "ORANGE"),
    (15000, "ORANGE PLUS"),
    (16000, "PiNK"),
    (17000, "PiNK PLUS"),
    (18000, "MURASAKi"),
    (18500, "MURASAKi PLUS"),
    (19000, "MiLK"),
    (19500, "MiLK PLUS"),
    (19900, "FiNALE"),
    (20000, "DX"),
    (20500, "DX PLUS"),
    (21000, "Splash"),
    (21500, "Splash PLUS"),
    (22000, "UNiVERSE"),
    (22500, "UNiVERSE PLUS"),
    (23000, "FESTiVAL"),
    (23500, "FESTiVAL PLUS"),
    (24000, "BUDDiES"),
    (24500, "BUDDiES PLUS"),
    (25000, "PRiSM"),
    (25500, "PRiSM PLUS"),
    (26000, "CiRCLE"),
    (26500, "CiRCLE PLUS"),
    (27000, "MAGiCAL"),
]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalized(text) -> str:
    return unicodedata.normalize("NFKC", str(text)).casefold().strip()


def override_index(path: Path) -> tuple[dict, list]:
    """Index remy_release_overrides.json ({"overrides": [...]}) by chart_key."""
    if not path.exists():
        return {}, []
    result, errors = {}, []
    for record in load(path)["overrides"]:
        chart_key = record.get("chart_key")
        if not chart_key:
            errors.append({"kind": "invalid_release_override", "record": record})
        elif chart_key in result:
            errors.append({"kind": "duplicate_release_override", "chart_key": chart_key})
        else:
            result[chart_key] = record
    return result, errors


def build(catalog_dir: Path) -> dict:
    """The default public compiler emits the flat, gated schema 2 catalog."""
    try:
        from .catalog_chart_compiler import build as build_charts
    except ImportError:
        from catalog_chart_compiler import build as build_charts
    return build_charts(catalog_dir)


def search(catalog_dir: Path, query: str, limit: int = 20, *, id_only: bool = False) -> list:
    connection = sqlite3.connect(f"file:{catalog_dir / 'catalog.sqlite'}?mode=ro", uri=True)
    query = normalized(query)
    if not query:
        connection.close()
        return []
    charts = [json.loads(row[0]) for row in connection.execute("SELECT data_json FROM charts")]
    connection.close()

    def matches(chart):
        numeric_match = query.isdecimal() and (
            chart["id"] == int(query)
            or any(str(row["id"]) == str(int(query)) for row in chart["external_ids"])
        )
        if id_only:
            return numeric_match
        terms = [chart["title"], *chart["aliases"]]
        return numeric_match or any(query in normalized(term) for term in terms)

    return sorted(
        (chart for chart in charts if matches(chart)),
        key=lambda chart: (normalized(chart["title"]) != query, chart["id"], chart["difficulty"]),
    )[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--search",
        help="Search the existing SQLite catalog by title, alias, LXNS ID, or verified Diving-Fish ID",
    )
    parser.add_argument(
        "--id",
        dest="song_id",
        type=int,
        help="Look up an existing LXNS ID or mapped Diving-Fish ID (same as --search)",
    )
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        if args.search is not None or args.song_id is not None:
            result = search(
                args.catalog_dir.resolve(),
                str(args.song_id) if args.song_id is not None else args.search,
                max(1, args.limit),
                id_only=args.song_id is not None,
            )
        else:
            result = build(args.catalog_dir.resolve())["counts"]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
