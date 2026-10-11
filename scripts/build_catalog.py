#!/usr/bin/env python3
"""Build a deterministic offline catalog from saved API responses (no network).

Build:  python3 scripts/build_catalog.py
Search: python3 scripts/build_catalog.py --search 'Secret Sleuth'
        python3 scripts/build_catalog.py --id 10030

All release candidates remain separate from verified Japanese chart releases.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import sqlite3
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

try:
    from .catalog_policy import ordinary_scope, remaster_scope, source_policy
    from .catalog_record_review import prepare_record_review
    from .catalog_versions import empty_release, resolve_versions
except ImportError:
    from catalog_policy import ordinary_scope, remaster_scope, source_policy
    from catalog_record_review import prepare_record_review
    from catalog_versions import empty_release, resolve_versions


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "data" / "catalog"
DIFFICULTIES = {0: "BASIC", 1: "ADVANCED", 2: "EXPERT", 3: "MASTER", 4: "Re:MASTER"}
NOTE_PARTS = ("tap", "hold", "slide", "touch", "break")
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


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized(text) -> str:
    return unicodedata.normalize("NFKC", str(text)).casefold().strip()


def jp_version_name(code) -> str | None:
    try:
        value = int(code)
    except (TypeError, ValueError):
        return None
    # Preserve unfamiliar future codes rather than assigning the newest known family.
    if value < 10000 or value >= 27500:
        return None
    return next(
        (name for boundary, name in reversed(JP_VERSION_BOUNDARIES) if value >= boundary), None
    )


def dump(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


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


def validate_notes(notes, chart_key: str, issues: list) -> None:
    if not isinstance(notes, dict):
        issues.append({"kind": "invalid_notes_shape", "chart_key": chart_key, "notes": notes})
        return
    if "left" in notes or "right" in notes:
        if set(notes) != {"left", "right"}:
            issues.append({"kind": "invalid_buddy_notes_shape", "chart_key": chart_key})
        for side in ("left", "right"):
            validate_notes(notes.get(side), f"{chart_key}/{side}", issues)
        return
    if not all(
        isinstance(notes.get(key), int) and notes[key] >= 0 for key in ("total", *NOTE_PARTS)
    ):
        issues.append({"kind": "invalid_note_counts", "chart_key": chart_key, "notes": notes})
    elif notes["total"] != sum(notes[key] for key in NOTE_PARTS):
        issues.append({"kind": "note_total_mismatch", "chart_key": chart_key, "notes": notes})


def verified_release(record: dict | None, *, chart_type=None, difficulty_index=None) -> dict:
    empty = {
        "jp_release_version": None,
        "jp_release_date": None,
        "jp_release_source": None,
        "jp_release_provider": None,
        "jp_release_evidence": None,
        "release_status": "unresolved",
        "release_source_scope": None,
    }
    if not record:
        return empty
    if chart_type is None or difficulty_index is None:
        parts = str(record.get("chart_key", "")).split(":")
        chart_type = (
            chart_type
            or record.get("type", record.get("chart_type"))
            or (parts[1] if len(parts) == 3 else None)
        )
        if difficulty_index is None:
            difficulty_index = record.get(
                "difficulty_index", record.get("difficulty", parts[2] if len(parts) == 3 else None)
            )
            try:
                difficulty_index = int(difficulty_index)
            except (TypeError, ValueError):
                return empty
    if not remaster_scope(chart_type, difficulty_index):
        return empty
    version = record.get("jp_release_version", record.get("release_version", record.get("version")))
    source = record.get(
        "jp_release_source", record.get("source_url", record.get("source", record.get("sources")))
    )
    status = record.get(
        "release_status",
        record.get("status", "verified" if record.get("verified") is True else "unresolved"),
    )
    # The override file is explicitly a curated, evidence-backed release index.
    # An unresolved record must never be silently promoted to verified.
    if (
        not isinstance(version, str)
        or not version
        or not isinstance(source, str)
        or not source
        or status != "verified"
        or record.get("verified") is False
    ):
        return empty
    provider = {
        "silentblue.remywiki.com": "remywiki",
        "info-maimai.sega.jp": "sega",
        "maimai.sega.jp": "sega",
        "www.sega.jp": "sega",
        "sega.jp": "sega",
        "gamerch.com": "gamerch",
    }.get(urlparse(source).hostname)
    release_date = record.get("jp_release_date", record.get("release_date", record.get("date")))
    if provider != "remywiki" or record.get("source_provider", "remywiki") != "remywiki":
        return empty
    if not release_date:
        return empty
    if not record.get("evidence"):
        raise ValueError(
            f"Verified release requires a supported source and evidence: {record.get('chart_key')}"
        )
    if release_date:
        datetime.date.fromisoformat(release_date)
    return {
        "jp_release_version": version,
        "jp_release_date": release_date,
        "jp_release_source": source,
        "jp_release_provider": provider,
        "jp_release_evidence": record["evidence"],
        "release_status": "verified",
        "release_source_scope": "chart",
    }


def build_v1(catalog_dir: Path) -> dict:
    raw = catalog_dir / "raw"
    effective_path = catalog_dir / "effective_lxns_songs.json"
    lxns = load(effective_path if effective_path.exists() else raw / "lxns_songs.json")
    alias_records = load(raw / "lxns_aliases.json")["aliases"]
    fish = load(raw / "divingfish_music_data.json")
    arcade = (
        load(raw / "arcade_songs.json") if (raw / "arcade_songs.json").exists() else {"songs": []}
    )
    previous = (
        load(catalog_dir / "previous_catalog.json")
        if (catalog_dir / "previous_catalog.json").exists()
        else None
    )
    adjudications = (
        load(catalog_dir / "catalog_adjudications.json")
        if (catalog_dir / "catalog_adjudications.json").exists()
        else None
    )
    remy_conflicts = (
        load(catalog_dir / "remy_conflicts.json")
        if (catalog_dir / "remy_conflicts.json").exists()
        else None
    )
    sources = load(raw / "sources.json")
    overrides, override_errors = override_index(catalog_dir / "remy_release_overrides.json")
    supplements, supplement_errors = override_index(
        catalog_dir / "supplemental_release_overrides.json"
    )
    cross_validation_issues = list(supplement_errors)
    for key, record in supplements.items():
        if key in overrides:
            fields = ("jp_release_version", "jp_release_date")
            if any(record.get(field) != overrides[key].get(field) for field in fields):
                cross_validation_issues.append(
                    {
                        "kind": "release_source_conflict",
                        "chart_key": key,
                        "remy": overrides[key],
                        "supplement": record,
                    }
                )
    jacket_manifest_path = catalog_dir / "jacket_manifest.json"
    jacket_manifest = load(jacket_manifest_path) if jacket_manifest_path.exists() else {}
    jacket_metadata = {record["song_id"]: record for record in jacket_manifest.get("jackets", [])}
    versions = resolve_versions(
        lxns, arcade, list(overrides.values()), previous, adjudications, remy_conflicts
    )
    dump(catalog_dir / "catalog_review.json", {k: v for k, v in versions.items() if k != "objects"})
    issues, fatal = cross_validation_issues + versions["issues"], override_errors
    inputs = []
    for path in sorted(raw.glob("*.json")):
        inputs.append(
            {"path": f"raw/{path.name}", "sha256": digest(path), "bytes": path.stat().st_size}
        )
    for name in (
        "remy_release_overrides.json",
        "supplemental_release_overrides.json",
        "jacket_manifest.json",
        "effective_lxns_songs.json",
        "retained_lxns_records.json",
        "catalog_diff.json",
        "pipeline_report.json",
        "previous_catalog.json",
        "catalog_adjudications.json",
        "catalog_review.json",
        "remy_conflicts.json",
    ):
        path = catalog_dir / name
        if path.exists():
            inputs.append({"path": name, "sha256": digest(path), "bytes": path.stat().st_size})
    for source in sources:
        path = raw / source["file"]
        if not path.exists() or digest(path) != source["sha256"]:
            fatal.append({"kind": "source_hash_mismatch", "file": source["file"]})

    fish_by_id = collections.defaultdict(list)
    for record in fish:
        fish_by_id[int(record["id"])].append(record)
    aliases = collections.defaultdict(list)
    for record in alias_records:
        aliases[int(record["song_id"])].extend(record["aliases"])

    songs, seen_ids, seen_keys, used_fish = [], set(), set(), set()
    type_counts, difficulty_counts, release_counts = (
        collections.Counter(),
        collections.Counter(),
        collections.Counter(),
    )
    remaster_unresolved, missing_jackets, matched_type_count = [], [], 0
    for original in sorted(lxns["songs"], key=lambda song: song["id"]):
        song_id = original["id"]
        if song_id in seen_ids:
            fatal.append({"kind": "duplicate_song_id", "song_id": song_id})
        seen_ids.add(song_id)
        jacket_path = f"jackets/{song_id}.png"
        jacket_source = jacket_metadata.get(song_id)
        jacket_available = (catalog_dir / jacket_path).is_file() and (
            not jacket_source or jacket_source.get("status") == "ok"
        )
        if not jacket_available:
            missing_jackets.append(song_id)
        sega_match = {"status": "disabled", "artist_matches": None, "scope": None, "candidates": []}
        song = {
            "id": song_id,
            "source_identity": {
                "id": song_id,
                "title": original["title"],
                "artist": original["artist"],
            },
            "title": original["title"],
            "artist": original["artist"],
            "bpm": original["bpm"],
            "genre": original["genre"],
            "aliases": sorted(
                set(aliases.get(song_id, [])), key=lambda item: (normalized(item), item)
            ),
            "jacket_path": jacket_path,
            "jacket_available": jacket_available,
            "jacket_source": jacket_source,
            "api_version": original["version"],
            "rights": original.get("rights"),
            "locked": original.get("locked", False),
            "upstream_status": original.get("upstream_status", "present"),
            "upstream_missing_since": original.get("upstream_missing_since"),
            "map": original.get("map"),
            "external_ids": [{"namespace": "lxns", "id": str(song_id), "type": None}],
            "jp_song_version_candidate": None,
            "jp_song_version_code": None,
            "jp_song_version_source": None,
            "jp_song_version_match": sega_match,
            "sega_jp_types": [],
            "charts": [],
        }
        for chart_type, source_charts in original["difficulties"].items():
            if not source_charts:
                continue
            external_id = song_id + 10000 if chart_type == "dx" else song_id
            expected_type = "SD" if chart_type == "standard" else "DX"
            matches = [
                record
                for record in fish_by_id.get(external_id, [])
                if record["type"] == expected_type
            ]
            type_record = matches[0] if len(matches) == 1 else None
            match_rule = {
                "standard": "same_id_standard",
                "dx": "lxns_id_plus_10000",
                "utage": "same_id_utage_no_offset",
            }.get(chart_type)
            if not type_record:
                issues.append(
                    {
                        "kind": "external_type_match_missing_or_ambiguous",
                        "song_id": song_id,
                        "type": chart_type,
                        "expected_id": external_id,
                        "matches": len(matches),
                    }
                )
            else:
                matched_type_count += 1
                used_fish.add(str(type_record["id"]))
                song["external_ids"].append(
                    {
                        "namespace": "divingfish",
                        "id": str(external_id),
                        "type": chart_type,
                        "mapping_rule": match_rule,
                        "source_title": type_record["title"],
                    }
                )
                if type_record["title"] != original["title"]:
                    issues.append(
                        {
                            "kind": "external_title_difference",
                            "song_id": song_id,
                            "type": chart_type,
                            "lxns_title": original["title"],
                            "divingfish_title": type_record["title"],
                            "matched_by": match_rule,
                        }
                    )
            for source_chart in source_charts:
                index = source_chart["difficulty"]
                key = f"{song_id}:{chart_type}:{index}"
                if key in seen_keys:
                    fatal.append({"kind": "duplicate_chart_key", "chart_key": key})
                seen_keys.add(key)
                if source_chart["type"] != chart_type:
                    fatal.append(
                        {
                            "kind": "chart_type_inconsistent",
                            "chart_key": key,
                            "source_type": source_chart["type"],
                        }
                    )
                notes = source_chart.get("notes")
                validate_notes(notes, key, issues)
                object_key = (
                    f"{song_id}:{chart_type}:ordinary" if ordinary_scope(chart_type, index) else key
                )
                release = versions["objects"].get(object_key, empty_release())
                if key in overrides and not remaster_scope(chart_type, index):
                    issues.append(
                        {
                            "kind": "release_override_outside_remaster_scope",
                            "chart_key": key,
                            "retained_in": "remy_release_overrides.json",
                            "included_as_authority": False,
                        }
                    )
                if type_record:
                    fish_constants = type_record.get("ds", [])
                    if (
                        index < len(fish_constants)
                        and chart_type != "utage"
                        and fish_constants[index] != source_chart["level_value"]
                    ):
                        issues.append(
                            {
                                "kind": "external_constant_difference",
                                "chart_key": key,
                                "lxns_constant": source_chart["level_value"],
                                "divingfish_constant": fish_constants[index],
                                "authority": "lxns",
                            }
                        )
                candidate = None
                jp_chart_candidate = None
                chart = {
                    "chart_key": key,
                    "type": chart_type,
                    "difficulty_index": index,
                    "difficulty_name": "UTAGE"
                    if chart_type == "utage"
                    else DIFFICULTIES.get(index, f"UNKNOWN_{index}"),
                    "level": source_chart["level"],
                    "constant": None
                    if chart_type == "utage" and source_chart["level_value"] == 0
                    else source_chart["level_value"],
                    "constant_source": "lxns_current_cn",
                    "upstream_status": source_chart.get("upstream_status", "present"),
                    "upstream_missing_since": source_chart.get("upstream_missing_since"),
                    "note_designer": source_chart["note_designer"],
                    "notes": notes,
                    "api_version": source_chart["version"],
                    "divingfish_id": str(external_id) if type_record else None,
                    "id_mapping_rule": match_rule if type_record else None,
                    "type_release_version_candidate": candidate,
                    "type_release_version_source": "raw/divingfish_music_data.json#basic_info.from"
                    if candidate
                    else None,
                    "type_release_version_region": "CN" if candidate else None,
                    "type_release_version_scope": "cn_source_candidate" if candidate else None,
                    "jp_release_version_candidate": jp_chart_candidate,
                    "jp_release_candidate_source": None,
                    "jp_release_candidate_scope": None,
                    **release,
                }
                if chart_type == "utage":
                    chart.update(
                        {
                            field: source_chart.get(field)
                            for field in ("kanji", "description", "is_buddy")
                        }
                    )
                if remaster_scope(chart_type, index) and release["release_status"] not in (
                    "verified",
                    "manual",
                ):
                    remaster_unresolved.append(
                        {
                            "chart_key": key,
                            "song_id": song_id,
                            "title": original["title"],
                            "type": chart_type,
                            "type_release_version_candidate": candidate,
                        }
                    )
                song["charts"].append(chart)
                type_counts[chart_type] += 1
                difficulty_counts[chart["difficulty_name"]] += 1
                release_counts[chart["release_status"]] += 1
        song["charts"].sort(key=lambda chart: (chart["type"], chart["difficulty_index"]))
        songs.append(song)

    record_result = prepare_record_review(
        lxns, fish, arcade, alias_records, songs, versions, adjudications
    )
    songs = record_result["songs"]
    issues.extend(record_result["issues"])
    dump(catalog_dir / "catalog_record_review.json", record_result["review"])
    record_review_path = catalog_dir / "catalog_record_review.json"
    inputs.append(
        {
            "path": record_review_path.name,
            "sha256": digest(record_review_path),
            "bytes": record_review_path.stat().st_size,
        }
    )

    for record in fish:
        if str(record["id"]) not in used_fish:
            issues.append(
                {
                    "kind": "external_song_not_in_lxns",
                    "divingfish_id": record["id"],
                    "title": record["title"],
                    "type": record["type"],
                    "retained_in": "raw/divingfish_music_data.json",
                    "included_in_catalog": False,
                }
            )
    for song_id in sorted(set(aliases) - seen_ids):
        issues.append(
            {
                "kind": "alias_song_not_in_lxns",
                "song_id": song_id,
                "aliases": aliases[song_id],
                "retained_in": "raw/lxns_aliases.json",
            }
        )
    for key in sorted(set(overrides) - seen_keys):
        issues.append({"kind": "release_override_chart_not_in_lxns", "chart_key": key})
    raw_chart_count = sum(
        len(charts) for song in lxns["songs"] for charts in song["difficulties"].values()
    )
    built_chart_count = sum(len(song["charts"]) for song in songs)
    if len(songs) != len(lxns["songs"]) or raw_chart_count != built_chart_count:
        fatal.append({"kind": "catalog_count_mismatch"})
    counts = {
        "songs": len(songs),
        "charts": built_chart_count,
        "song_types": sum(
            bool(charts) for song in lxns["songs"] for charts in song["difficulties"].values()
        ),
        "dual_standard_dx_songs": sum(
            bool(song["difficulties"].get("standard")) and bool(song["difficulties"].get("dx"))
            for song in lxns["songs"]
        ),
        "aliases": sum(len(song["aliases"]) for song in songs),
        "songs_with_aliases": sum(bool(song["aliases"]) for song in songs),
        "charts_by_type": dict(sorted(type_counts.items())),
        "charts_by_difficulty": dict(sorted(difficulty_counts.items())),
        "release_status": dict(sorted(release_counts.items())),
        "remaster_verified": difficulty_counts["Re:MASTER"] - len(remaster_unresolved),
        "remaster_verified_by_provider": dict(
            sorted(
                collections.Counter(
                    chart["jp_release_provider"]
                    for song in songs
                    for chart in song["charts"]
                    if remaster_scope(chart["type"], chart["difficulty_index"])
                    and chart["release_status"] in ("verified", "manual")
                ).items()
            )
        ),
        "remaster_unresolved": len(remaster_unresolved),
        "buddy_charts": sum(
            bool(chart.get("is_buddy")) for song in songs for chart in song["charts"]
        ),
        "jackets_available": len(songs) - len(missing_jackets),
        "jackets_missing": len(missing_jackets),
        "sega_jp_song_matches": sum(
            song["jp_song_version_match"]["status"] == "matched" for song in songs
        ),
        "sega_jp_artist_matches": sum(
            song["jp_song_version_match"]["artist_matches"] is True for song in songs
        ),
        "sega_jp_song_versions": sum(
            song["jp_song_version_candidate"] is not None for song in songs
        ),
    }
    report = {
        "schema_version": 1,
        "status": "failed" if fatal else "ok",
        "source_policy": source_policy(),
        "decision_revision": versions["decision_revision"],
        "counts": counts,
        "validation": {
            "input_song_count": len(lxns["songs"]),
            "output_song_count": len(songs),
            "input_chart_count": raw_chart_count,
            "output_chart_count": built_chart_count,
            "unique_song_ids": len(seen_ids),
            "unique_chart_keys": len(seen_keys),
            "matched_divingfish_song_types": matched_type_count,
            "input_hashes_verified": not any(
                error["kind"] == "source_hash_mismatch" for error in fatal
            ),
        },
        "fatal_errors": fatal,
        "issues": issues,
        "unresolved_remaster_releases": remaster_unresolved,
        "missing_jacket_ids": missing_jackets,
        "inputs": inputs,
    }
    dump(catalog_dir / "build_report.json", report)
    if fatal:
        raise ValueError(f"Catalog validation failed: {len(fatal)} errors; see build_report.json")
    catalog = {
        "schema_version": 1,
        "snapshot_at": max((source.get("fetched_at", "") for source in sources), default=None),
        "data_region": "CN",
        "primary_source": "lxns",
        "source_policy": source_policy(),
        "decision_revision": versions["decision_revision"],
        "counts": counts,
        "sources": sources,
        "input_files": inputs,
        "versions_cn": lxns["versions"],
        "genres": lxns["genres"],
        "versions_jp": [{"version": code, "title": title} for code, title in JP_VERSION_BOUNDARIES],
        "release_policy": {
            "ordinary": "Standard/DX BASIC-MASTER use only arcade-songs sheets.version; title, artist and format must uniquely match.",
            "remaster": "Re:MASTER uses only independent Remy chart debut evidence. Missing evidence remains unresolved.",
            "conflicts": "Previously adopted values are retained with conflict status. Only user decisions resolve conflicts.",
            "manual": "Explicit user decisions are bound to original identity and source evidence; stale decisions require review.",
            "excluded": "CN versions, top-level song versions, international overrides and SEGA song records are never JP release fallbacks.",
        },
        "songs": songs,
    }
    dump(catalog_dir / "catalog.json", catalog)
    write_sqlite(catalog_dir / "catalog.sqlite", catalog)
    report["validation"].update({"sqlite_integrity_check": "ok", "sqlite_foreign_key_errors": 0})
    report["outputs"] = [
        {
            "path": name,
            "sha256": digest(catalog_dir / name),
            "bytes": (catalog_dir / name).stat().st_size,
        }
        for name in ("catalog.json", "catalog.sqlite")
    ]
    dump(catalog_dir / "build_report.json", report)
    return report


def build(catalog_dir: Path) -> dict:
    """The default public compiler emits the flat, gated schema 2 catalog."""
    try:
        from .catalog_chart_compiler import build as build_charts
    except ImportError:
        from catalog_chart_compiler import build as build_charts
    return build_charts(catalog_dir)


def write_sqlite(path: Path, catalog: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        CREATE TABLE songs (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, artist TEXT NOT NULL,
            bpm REAL, genre TEXT, jacket_path TEXT NOT NULL, jacket_available INTEGER NOT NULL,
            api_version INTEGER, rights TEXT, locked INTEGER, map_json TEXT,
            jp_song_version_candidate TEXT, jp_song_version_code TEXT,
            jp_song_version_source TEXT, jp_song_version_match_json TEXT, data_json TEXT NOT NULL
        );
        CREATE TABLE charts (
            chart_key TEXT PRIMARY KEY, song_id INTEGER NOT NULL REFERENCES songs(id),
            type TEXT NOT NULL, difficulty_index INTEGER NOT NULL, difficulty_name TEXT NOT NULL,
            level TEXT NOT NULL, constant REAL, note_designer TEXT, notes_json TEXT NOT NULL,
            api_version INTEGER, divingfish_id TEXT, id_mapping_rule TEXT,
            type_release_version_candidate TEXT, type_release_version_region TEXT, type_release_version_scope TEXT,
            jp_release_version_candidate TEXT, jp_release_candidate_source TEXT, jp_release_candidate_scope TEXT,
            jp_release_version TEXT, jp_release_date TEXT,
            jp_release_source_json TEXT, jp_release_provider TEXT, jp_release_evidence TEXT,
            release_status TEXT NOT NULL, release_source_scope TEXT,
            data_json TEXT NOT NULL, UNIQUE(song_id, type, difficulty_index)
        );
        CREATE TABLE aliases (song_id INTEGER NOT NULL REFERENCES songs(id), alias TEXT NOT NULL,
                              normalized_alias TEXT NOT NULL, PRIMARY KEY(song_id, alias));
        CREATE TABLE external_ids (
            namespace TEXT NOT NULL, external_id TEXT NOT NULL, song_id INTEGER NOT NULL REFERENCES songs(id),
            type TEXT, mapping_rule TEXT, source_title TEXT,
            PRIMARY KEY(namespace, external_id)
        );
        CREATE TABLE search_terms (song_id INTEGER NOT NULL REFERENCES songs(id), kind TEXT NOT NULL,
                                   term TEXT NOT NULL, normalized_term TEXT NOT NULL);
        CREATE INDEX search_terms_normalized ON search_terms(normalized_term);
        CREATE INDEX charts_by_song ON charts(song_id, type, difficulty_index);
        CREATE INDEX aliases_by_song ON aliases(song_id);
        CREATE INDEX external_id_lookup ON external_ids(external_id);
    """)

    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    for key, value in catalog.items():
        if key != "songs":
            connection.execute("INSERT INTO metadata VALUES (?, ?)", (key, encode(value)))
    for song in catalog["songs"]:
        values = (
            song["id"],
            song["title"],
            song["artist"],
            song["bpm"],
            song["genre"],
            song["jacket_path"],
            int(song["jacket_available"]),
            song["api_version"],
            song["rights"],
            int(song["locked"]),
            encode(song["map"]),
            song["jp_song_version_candidate"],
            song["jp_song_version_code"],
            song["jp_song_version_source"],
            encode(song["jp_song_version_match"]),
            encode(song),
        )
        connection.execute(f"INSERT INTO songs VALUES ({','.join('?' for _ in values)})", values)
        terms = [("title", song["title"])] + [("alias", alias) for alias in song["aliases"]]
        for kind, term in terms:
            connection.execute(
                "INSERT INTO search_terms VALUES (?, ?, ?, ?)",
                (song["id"], kind, term, normalized(term)),
            )
        for alias in song["aliases"]:
            connection.execute(
                "INSERT INTO aliases VALUES (?, ?, ?)", (song["id"], alias, normalized(alias))
            )
        for external in song["external_ids"]:
            connection.execute(
                "INSERT INTO external_ids VALUES (?, ?, ?, ?, ?, ?)",
                (
                    external["namespace"],
                    external["id"],
                    song["id"],
                    external["type"],
                    external.get("mapping_rule"),
                    external.get("source_title"),
                ),
            )
        for chart in song["charts"]:
            values = (
                chart["chart_key"],
                song["id"],
                chart["type"],
                chart["difficulty_index"],
                chart["difficulty_name"],
                chart["level"],
                chart["constant"],
                chart["note_designer"],
                encode(chart["notes"]),
                chart["api_version"],
                chart["divingfish_id"],
                chart["id_mapping_rule"],
                chart["type_release_version_candidate"],
                chart["type_release_version_region"],
                chart["type_release_version_scope"],
                chart["jp_release_version_candidate"],
                chart["jp_release_candidate_source"],
                chart["jp_release_candidate_scope"],
                chart["jp_release_version"],
                chart["jp_release_date"],
                encode(chart["jp_release_source"]),
                chart["jp_release_provider"],
                chart["jp_release_evidence"],
                chart["release_status"],
                chart["release_source_scope"],
                encode(chart),
            )
            connection.execute(
                f"INSERT INTO charts VALUES ({','.join('?' for _ in values)})", values
            )
    connection.commit()
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("SQLite integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("SQLite foreign_key_check failed")
    connection.close()
    temporary.replace(path)


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
