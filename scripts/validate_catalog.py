#!/usr/bin/env python3
"""Validate an isolated catalog, including authority, hashes, retained rows and SQLite."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from PIL import Image

try:
    from .build_catalog import normalized, override_index
    from .catalog_policy import ordinary_scope, remaster_scope, source_policy
    from .catalog_record_review import prepare_record_review
    from .catalog_versions import RELEASE_FIELDS, empty_release, resolve_versions
except ImportError:
    from build_catalog import normalized, override_index
    from catalog_policy import ordinary_scope, remaster_scope, source_policy
    from catalog_record_review import prepare_record_review
    from catalog_versions import RELEASE_FIELDS, empty_release, resolve_versions

ROOT = Path(__file__).resolve().parents[1] / "data" / "catalog"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(f"Catalog validation failed: {message}")


def validate_v1(catalog_dir: Path = ROOT) -> dict:
    root = Path(catalog_dir)

    def read(name):
        return json.loads((root / name).read_text())

    catalog = read("catalog.json")
    raw = read("raw/lxns_songs.json")
    require(
        catalog.get("schema_version") == 1
        and catalog.get("primary_source") == "lxns"
        and catalog.get("data_region") == "CN",
        "metadata schema",
    )
    if "source_policy" in catalog:
        require(
            catalog["source_policy"] == source_policy(),
            "source policy differs from accepted authorities",
        )
        require(
            read("build_report.json").get("source_policy") == catalog["source_policy"],
            "build report source policy mismatch",
        )
    effective = (
        read("effective_lxns_songs.json") if (root / "effective_lxns_songs.json").exists() else raw
    )
    if effective is not raw:
        try:
            from .catalog_pipeline import merge_and_diff
        except ImportError:
            from catalog_pipeline import merge_and_diff
        retained = read("retained_lxns_records.json")
        prepared = read("pipeline_report.json")
        reconstructed, _ = merge_and_diff(raw, retained, prepared["started_at"])
        require(
            reconstructed == effective,
            "effective catalog is not the raw snapshot plus explicitly retained records",
        )
    originals = {song["id"]: song for song in effective["songs"]}
    songs = {song["id"]: song for song in catalog["songs"]}
    require(
        len(songs) == len(catalog["songs"]) == len(effective["songs"]), "duplicate/missing songs"
    )
    require(songs.keys() == originals.keys(), "song coverage differs from effective LXNS")
    for source in read("raw/sources.json"):
        path = root / "raw" / source["file"]
        require(
            path.is_file()
            and sha(path) == source["sha256"]
            and path.stat().st_size == source["bytes"],
            f"raw source hash: {source['file']}",
        )
    for item in catalog["input_files"]:
        path = root / item["path"]
        require(
            path.is_file() and sha(path) == item["sha256"] and path.stat().st_size == item["bytes"],
            f"build input hash: {item['path']}",
        )
    for item in read("build_report.json").get("outputs", []):
        require(sha(root / item["path"]) == item["sha256"], f"build output hash: {item['path']}")

    require(
        catalog["versions_cn"] == effective["versions"]
        and catalog["genres"] == effective["genres"],
        "LXNS version/genre authority",
    )
    aliases_by_song = {}
    for row in read("raw/lxns_aliases.json")["aliases"]:
        aliases_by_song.setdefault(row["song_id"], set()).update(row["aliases"])
    overrides, override_errors = override_index(root / "remy_release_overrides.json")
    require(not override_errors, "invalid Remy override index")
    versions = resolve_versions(
        effective,
        read("raw/arcade_songs.json")
        if (root / "raw/arcade_songs.json").exists()
        else {"songs": []},
        list(overrides.values()),
        read("previous_catalog.json") if (root / "previous_catalog.json").exists() else None,
        read("catalog_adjudications.json")
        if (root / "catalog_adjudications.json").exists()
        else None,
        read("remy_conflicts.json") if (root / "remy_conflicts.json").exists() else None,
    )
    require(catalog.get("decision_revision") == versions["decision_revision"], "decision revision")
    require(
        read("build_report.json").get("decision_revision") == versions["decision_revision"],
        "build report decision revision",
    )
    require(
        read("catalog_review.json") == {k: v for k, v in versions.items() if k != "objects"},
        "review output does not match evidence and decisions",
    )
    field_result = prepare_record_review(
        effective,
        read("raw/divingfish_music_data.json"),
        read("raw/arcade_songs.json")
        if (root / "raw/arcade_songs.json").exists()
        else {"songs": []},
        read("raw/lxns_aliases.json"),
        catalog["songs"],
        versions,
        read("catalog_adjudications.json")
        if (root / "catalog_adjudications.json").exists()
        else None,
    )
    require(
        read("catalog_record_review.json") == field_result["review"],
        "record review does not match source fields and decisions",
    )
    field_songs = {song["id"]: song for song in field_result["songs"]}
    charts_by_key = {}
    for sid, song in songs.items():
        original = originals[sid]
        field_song = field_songs[sid]
        require(
            song.get("source_identity") == {k: original[k] for k in ("id", "title", "artist")},
            f"{sid}: raw source identity",
        )
        require(
            song.get("field_overrides") == field_song.get("field_overrides"),
            f"{sid}: field override provenance",
        )
        for field in ("title", "artist", "bpm", "genre"):
            require(song[field] == field_song[field], f"{sid}: {field}")
        require(song["api_version"] == original["version"], f"{sid}: version")
        for field, default in (("rights", None), ("locked", False), ("map", None)):
            require(song[field] == original.get(field, default), f"{sid}: {field} authority")
        expected_aliases = sorted(
            aliases_by_song.get(sid, set()), key=lambda item: (normalized(item), item)
        )
        require(song["aliases"] == expected_aliases, f"{sid}: LXNS alias authority")
        require(
            all(
                song[field] is None
                for field in (
                    "jp_song_version_candidate",
                    "jp_song_version_code",
                    "jp_song_version_source",
                )
            ),
            f"{sid}: legacy SEGA fields must stay empty",
        )
        require(song["sega_jp_types"] == [], f"{sid}: legacy SEGA types must stay empty")
        require(
            song.get("upstream_status", "present") == original.get("upstream_status", "present"),
            f"{sid}: upstream status",
        )
        expected = {
            f"{sid}:{kind}:{chart['difficulty']}": chart
            for kind, charts in original["difficulties"].items()
            for chart in charts
        }
        require(
            len(song["charts"]) == len(expected)
            and {c["chart_key"] for c in song["charts"]} == expected.keys(),
            f"{sid}: chart coverage",
        )
        field_charts = {chart["chart_key"]: chart for chart in field_song["charts"]}
        for chart in song["charts"]:
            key, source = chart["chart_key"], expected[chart["chart_key"]]
            require(key not in charts_by_key, f"duplicate chart: {key}")
            charts_by_key[key] = chart
            field_chart = field_charts[key]
            for field in ("type", "level", "notes", "note_designer"):
                require(
                    chart[field] == (source[field] if field == "type" else field_chart[field]),
                    f"{key}: {field}",
                )
            require(
                chart["constant"] == field_chart["constant"]
                and chart["constant_source"] == field_chart["constant_source"],
                f"{key}: constant authority",
            )
            require(
                chart.get("field_overrides") == field_chart.get("field_overrides"),
                f"{key}: field override provenance",
            )
            require(
                chart["api_version"] == source["version"]
                and chart["difficulty_index"] == source["difficulty"],
                f"{key}: version/difficulty",
            )
            require(
                chart.get("upstream_status", "present") == source.get("upstream_status", "present"),
                f"{key}: upstream status",
            )
            kind, difficulty = chart["type"], chart["difficulty_index"]
            object_key = f"{sid}:{kind}:ordinary" if ordinary_scope(kind, difficulty) else key
            release = versions["objects"].get(object_key, empty_release())
            require(
                all(chart.get(field) == release[field] for field in RELEASE_FIELDS),
                f"{key}: JP evidence/adjudication output mismatch",
            )
            require(
                all(
                    chart[field] is None
                    for field in (
                        "jp_release_version_candidate",
                        "jp_release_candidate_source",
                        "jp_release_candidate_scope",
                        "type_release_version_candidate",
                        "type_release_version_source",
                        "type_release_version_region",
                        "type_release_version_scope",
                    )
                ),
                f"{key}: legacy CN/SEGA version fallbacks must remain empty",
            )
            if chart["jp_release_version"]:
                allowed = (
                    ("manual", "arcade_songs")
                    if ordinary_scope(kind, difficulty)
                    else ("manual", "remywiki")
                    if remaster_scope(kind, difficulty)
                    else ()
                )
                require(chart["jp_release_provider"] in allowed, f"{key}: JP authority scope")
    require(
        catalog["counts"]["songs"] == len(songs)
        and catalog["counts"]["charts"] == len(charts_by_key),
        "catalog counters",
    )

    jackets = read("jacket_manifest.json")
    image_records = {row["song_id"]: row for row in jackets["jackets"]}
    require(
        len(image_records) == len(jackets["jackets"]) == len(songs)
        and image_records.keys() == songs.keys(),
        "jacket manifest coverage",
    )
    dimensions, decoded, missing = set(), 0, []
    for sid, row in image_records.items():
        require(
            row["asset_id"] == sid % 10000 and row["file"] == f"jackets/{sid}.png",
            f"{sid}: jacket mapping",
        )
        if row["status"] != "ok":
            require(not songs[sid]["jacket_available"], f"{sid}: failed jacket cannot be available")
            missing.append(sid)
            continue
        path = root / row["file"]
        require(
            path.is_file() and path.stat().st_size == row["bytes"] and sha(path) == row["sha256"],
            f"{sid}: jacket hash",
        )
        with Image.open(path) as picture:
            require(
                picture.format == "PNG" and picture.size == (row["width"], row["height"]),
                f"{sid}: jacket format",
            )
            picture.load()
            dimensions.add(picture.size)
        decoded += 1
        require(
            songs[sid]["jacket_available"] and songs[sid]["jacket_path"] == row["file"],
            f"{sid}: jacket availability",
        )
    require(catalog["counts"]["jackets_missing"] == len(missing), "missing jacket counter")

    evidence_files, retired = [], []
    for name in ("remy_release_overrides.json", "supplemental_release_overrides.json"):
        if not (root / name).exists():
            continue
        evidence_files.append(name)
        doc = read(name)
        for receipt in doc.get("evidence_files", []):
            require(
                sha(root / receipt["file"]) == receipt["sha256"],
                f"evidence hash: {receipt['file']}",
            )

    def check_page_evidence(key, row):
        if row.get("evidence_file") and row.get("evidence_sha256"):
            path = root / row["evidence_file"]
            require(
                path.is_file() and sha(path) == row["evidence_sha256"], f"{key}: page evidence hash"
            )

    for key, row in overrides.items():
        check_page_evidence(key, row)
        if key not in charts_by_key:
            retired.append(key)
    if (root / "remy_conflicts.json").exists():
        evidence_files.append("remy_conflicts.json")
        for conflict in read("remy_conflicts.json").get("conflicts", []):
            for row in conflict.get("candidates", []):
                check_page_evidence(conflict["chart_key"], row)

    with closing(sqlite3.connect(f"file:{root / 'catalog.sqlite'}?mode=ro", uri=True)) as conn:
        require(conn.execute("PRAGMA integrity_check").fetchone() == ("ok",), "SQLite integrity")
        require(not conn.execute("PRAGMA foreign_key_check").fetchall(), "SQLite foreign keys")
        require(
            conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == len(songs),
            "SQLite song count",
        )
        require(
            conn.execute("SELECT COUNT(*) FROM charts").fetchone()[0] == len(charts_by_key),
            "SQLite chart count",
        )
        expected_columns = {
            "songs": {
                "id",
                "title",
                "artist",
                "bpm",
                "genre",
                "jacket_path",
                "jacket_available",
                "api_version",
                "rights",
                "locked",
                "map_json",
                "jp_song_version_candidate",
                "jp_song_version_code",
                "jp_song_version_source",
                "jp_song_version_match_json",
                "data_json",
            },
            "charts": {
                "chart_key",
                "song_id",
                "type",
                "difficulty_index",
                "difficulty_name",
                "level",
                "constant",
                "note_designer",
                "notes_json",
                "api_version",
                "divingfish_id",
                "id_mapping_rule",
                "type_release_version_candidate",
                "type_release_version_region",
                "type_release_version_scope",
                "jp_release_version_candidate",
                "jp_release_candidate_source",
                "jp_release_candidate_scope",
                "jp_release_version",
                "jp_release_date",
                "jp_release_source_json",
                "jp_release_provider",
                "jp_release_evidence",
                "release_status",
                "release_source_scope",
                "data_json",
            },
        }
        for table, columns in expected_columns.items():
            require(
                {row[1] for row in conn.execute(f"PRAGMA table_info({table})")} == columns,
                f"SQLite {table} column schema",
            )
        conn.row_factory = sqlite3.Row
        chart_song_ids = {
            chart["chart_key"]: sid for sid, song in songs.items() for chart in song["charts"]
        }
        for table, key_name, payloads in (
            ("songs", "id", songs),
            ("charts", "chart_key", charts_by_key),
        ):
            for row in conn.execute(f"SELECT * FROM {table}"):
                key = row[key_name]
                require(key in payloads, f"{key}: SQLite {table} unknown key")
                expected = payloads[key]
                require(json.loads(row["data_json"]) == expected, f"{key}: SQLite {table} payload")
                for field in row.keys():
                    if field == "data_json":
                        continue
                    value = row[field]
                    if field == "song_id":
                        wanted = chart_song_ids[key]
                    elif field.endswith("_json"):
                        wanted = expected[field[:-5]]
                        value = json.loads(value)
                    else:
                        wanted = expected[field]
                    if isinstance(wanted, bool):
                        wanted = int(wanted)
                    require(value == wanted, f"{key}: SQLite {table}.{field} column mismatch")
        metadata = {
            key: json.loads(value)
            for key, value in conn.execute("SELECT key, value_json FROM metadata")
        }
        require(
            metadata == {key: value for key, value in catalog.items() if key != "songs"},
            "SQLite metadata",
        )
    report = {
        "status": "ok",
        "songs_checked": len(songs),
        "charts_checked": len(charts_by_key),
        "images_decoded_and_hash_checked": decoded,
        "jackets_missing": missing,
        "distinct_jacket_resource_ids": len({row["asset_id"] for row in image_records.values()}),
        "image_dimensions": [list(size) for size in sorted(dimensions)],
        "local_jacket_bytes": sum(
            row.get("bytes", 0) for row in image_records.values() if row["status"] == "ok"
        ),
        "remaster_verified": catalog["counts"]["remaster_verified"],
        "remaster_verified_by_provider": catalog["counts"]["remaster_verified_by_provider"],
        "remaster_unresolved": catalog["counts"]["remaster_unresolved"],
        "retired_remy_records": retired,
        "sqlite_integrity": "ok",
        "sqlite_foreign_key_errors": 0,
        "input_sha256": {
            name: sha(root / name)
            for name in ["catalog.json", "catalog.sqlite", "jacket_manifest.json", *evidence_files]
        },
    }
    try:
        from .catalog_pipeline import atomic_json
    except ImportError:
        from catalog_pipeline import atomic_json
    atomic_json(root / "validation_report.json", report)
    return report


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
