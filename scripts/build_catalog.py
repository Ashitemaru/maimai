#!/usr/bin/env python3
"""Build a deterministic offline catalog from saved API responses (no network).

Build:  python3 scripts/build_catalog.py
Search: python3 scripts/build_catalog.py --search 'Secret Sleuth'
        python3 scripts/build_catalog.py --id 10030

All release candidates remain separate from verified Japanese chart releases.
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
    """Accept a list, {'overrides': list/dict}, or a chart-keyed dictionary."""
    if not path.exists():
        return {}, []
    document = load(path)
    records = document
    if isinstance(document, dict):
        records = document.get(
            "overrides", document.get("charts", document.get("records", document))
        )
    if isinstance(records, dict):
        records = [
            dict(value, chart_key=key)
            for key, value in records.items()
            if isinstance(value, dict) and ":" in key
        ]
    result, errors = {}, []
    for record in records:
        chart_key = record.get("chart_key")
        if not chart_key and record.get("song_id") is not None:
            chart_type = record.get("type", record.get("chart_type"))
            index = record.get("difficulty_index", record.get("difficulty", 4))
            chart_key = f"{record['song_id']}:{chart_type}:{index}"
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
    schema = connection.execute(
        "SELECT value_json FROM metadata WHERE key='schema_version'"
    ).fetchone()
    if schema and json.loads(schema[0]) == 2:
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
            key=lambda chart: (
                normalized(chart["title"]) != query,
                chart["id"],
                chart["difficulty"],
            ),
        )[:limit]
    id_query = str(int(query)) if query.isdecimal() else query
    if query.isdecimal():
        ids = connection.execute(
            "SELECT DISTINCT song_id FROM external_ids WHERE external_id = ? ORDER BY song_id LIMIT ?",
            (id_query, limit),
        ).fetchall()
        if not id_only:
            # Numeric titles such as "39" are valid song names as well as potential IDs.
            title_ids = connection.execute(
                "SELECT DISTINCT song_id FROM search_terms WHERE normalized_term = ? ORDER BY song_id LIMIT ?",
                (query, limit),
            ).fetchall()
            ids = list(dict.fromkeys(ids + title_ids))[:limit]
    else:
        ids = connection.execute(
            """
            SELECT song_id FROM search_terms WHERE instr(normalized_term, ?) > 0
            GROUP BY song_id ORDER BY MAX(normalized_term = ?) DESC, MIN(length(normalized_term)), song_id LIMIT ?
        """,
            (query, query, limit),
        ).fetchall()
    result = []
    for (song_id,) in ids:
        song = json.loads(
            connection.execute("SELECT data_json FROM songs WHERE id = ?", (song_id,)).fetchone()[0]
        )
        matching_types = {
            row[0]
            for row in connection.execute(
                "SELECT type FROM external_ids WHERE song_id = ? AND external_id = ?",
                (song_id, id_query),
            )
        }
        charts = (
            song["charts"]
            if None in matching_types or not matching_types
            else [chart for chart in song["charts"] if chart["type"] in matching_types]
        )
        result.append(
            {
                "id": song_id,
                "title": song["title"],
                "artist": song["artist"],
                "aliases": song["aliases"],
                "jacket_path": song["jacket_path"],
                "jp_song_version_candidate": song["jp_song_version_candidate"],
                "jp_song_version_code": song["jp_song_version_code"],
                "charts": charts,
            }
        )
    connection.close()
    return result


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
