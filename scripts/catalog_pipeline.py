#!/usr/bin/env python3
"""Prepare one isolated public catalog snapshot; never fetch Remy or personal scores."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

try:
    from .download_jackets import BASE, RESOURCE_MAPPING_SOURCE, inspect
except ImportError:
    from download_jackets import BASE, RESOURCE_MAPPING_SOURCE, inspect

SOURCES = (
    (
        "lxns_songs.json",
        "https://maimai.lxns.net/api/v0/maimai/song/list?notes=true",
        "songs",
        True,
    ),
    ("lxns_aliases.json", "https://maimai.lxns.net/api/v0/maimai/alias/list", "aliases", True),
    (
        "divingfish_music_data.json",
        "https://www.diving-fish.com/api/maimaidxprober/music_data",
        None,
        False,
    ),
    ("arcade_songs.json", "https://dp4p6x0xfi5o9.cloudfront.net/maimai/data.json", "songs", False),
)


def now():
    return datetime.now(UTC).isoformat()


def atomic_bytes(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def atomic_json(path: Path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def read(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def fetch(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "maimai-cover-local-catalog/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("Public source exceeded 16 MiB limit")
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() in ("content-type", "etag", "last-modified")
        }
    return data, headers


def valid_notes(notes):
    if not isinstance(notes, dict):
        return False
    if "left" in notes or "right" in notes:
        return set(notes) == {"left", "right"} and all(
            valid_notes(notes[k]) for k in ("left", "right")
        )
    fields = ("tap", "hold", "slide", "touch", "break")
    return all(type(notes.get(k)) is int and notes[k] >= 0 for k in ("total", *fields)) and notes[
        "total"
    ] == sum(notes[k] for k in fields)


def check_source(filename, value, *, allow_empty=False):
    """Reject malformed authoritative responses before replacing any public snapshot."""
    if filename == "lxns_songs.json":
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("songs"), list)
            or not value["songs"]
        ):
            raise ValueError("LXNS songs response is empty or malformed")
        if not isinstance(value.get("versions"), list) or not isinstance(value.get("genres"), list):
            raise ValueError("LXNS version/genre lists are missing")
        seen, keys = set(), set()
        for song in value["songs"]:
            if (
                not isinstance(song, dict)
                or not isinstance(song.get("id"), int)
                or song["id"] in seen
            ):
                raise ValueError("LXNS song IDs are invalid or duplicated")
            seen.add(song["id"])
            for field in ("title", "artist", "genre"):
                if not isinstance(song.get(field), str):
                    raise ValueError(f"LXNS song {song['id']} lacks {field}")
            for field in ("bpm", "version"):
                if type(song.get(field)) not in (int, float) or not math.isfinite(song[field]):
                    raise ValueError(f"LXNS song {song['id']} has invalid {field}")
            if not isinstance(song.get("difficulties"), dict):
                raise ValueError("LXNS difficulties are malformed")
            for kind, charts in song["difficulties"].items():
                if kind not in ("standard", "dx", "utage") or not isinstance(charts, list):
                    raise ValueError("Unknown LXNS chart format")
                for chart in charts:
                    key = (song["id"], kind, chart.get("difficulty"))
                    if (
                        chart.get("type") != kind
                        or not isinstance(chart.get("difficulty"), int)
                        or key in keys
                    ):
                        raise ValueError(f"Invalid or duplicate LXNS chart {key}")
                    keys.add(key)
                    if (
                        type(chart.get("level_value")) not in (int, float)
                        or not math.isfinite(chart["level_value"])
                        or not isinstance(chart.get("level"), str)
                    ):
                        raise ValueError(f"LXNS chart level is malformed: {key}")
                    if not isinstance(chart.get("version"), int) or not valid_notes(
                        chart.get("notes")
                    ):
                        raise ValueError(f"LXNS chart version/notes are malformed: {key}")
                    if not isinstance(chart.get("note_designer"), str):
                        raise ValueError(f"LXNS chart note designer is malformed: {key}")
        return len(value["songs"])
    if filename == "lxns_aliases.json":
        if not isinstance(value, dict) or not isinstance(value.get("aliases"), list):
            raise ValueError("LXNS aliases response is malformed")
        for row in value["aliases"]:
            if (
                not isinstance(row.get("song_id"), int)
                or not isinstance(row.get("aliases"), list)
                or not all(isinstance(a, str) for a in row["aliases"])
            ):
                raise ValueError("LXNS alias row is malformed")
        return len(value["aliases"])
    if filename == "arcade_songs.json":
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("songs"), list)
            or (not value["songs"] and not allow_empty)
        ):
            raise ValueError("arcade-songs response is empty or malformed")
        seen = set()
        for row in value["songs"]:
            if (
                not isinstance(row, dict)
                or not all(isinstance(row.get(k), str) for k in ("songId", "title", "artist"))
                or not isinstance(row.get("sheets"), list)
            ):
                raise ValueError("arcade-songs song metadata is malformed")
            if not row["songId"] or row["songId"] in seen:
                raise ValueError("arcade-songs song IDs are invalid or duplicated")
            seen.add(row["songId"])
            sheets = set()
            for sheet in row["sheets"]:
                if (
                    not isinstance(sheet, dict)
                    or sheet.get("type") not in ("std", "dx", "utage")
                    or not isinstance(sheet.get("difficulty"), str)
                ):
                    raise ValueError("arcade-songs sheet type/difficulty is malformed")
                if sheet.get("version") is not None and not isinstance(sheet["version"], str):
                    raise ValueError("arcade-songs sheet version is malformed")
                if sheet["type"] != "utage":
                    key = (sheet["type"], sheet["difficulty"])
                    if (
                        sheet["difficulty"]
                        not in ("basic", "advanced", "expert", "master", "remaster")
                        or key in sheets
                    ):
                        raise ValueError("arcade-songs sheet difficulty is unknown or duplicated")
                    sheets.add(key)
        return len(value["songs"])
    if not isinstance(value, list):
        raise ValueError(f"{filename} is not a list")
    for row in value:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("title"), str)
            or not isinstance(row.get("artist", row.get("basic_info", {}).get("artist")), str)
        ):
            raise ValueError(f"{filename} has malformed song metadata")
        if filename == "divingfish_music_data.json":
            int(row["id"])
            if (
                row.get("type") not in ("SD", "DX")
                or not isinstance(row.get("basic_info"), dict)
                or not isinstance(row.get("ds", []), list)
            ):
                raise ValueError("Waterfish song shape is malformed")
    return len(value)


def chart_index(document):
    return {
        f"{song['id']}:{kind}:{chart['difficulty']}": (song, chart)
        for song in document.get("songs", [])
        for kind, charts in song["difficulties"].items()
        for chart in charts
    }


def merge_and_diff(current: dict, previous: dict, timestamp: str):
    """Keep disappearing records explicitly, without altering the raw authority snapshot."""
    effective = copy.deepcopy(current)
    old_songs = {s["id"]: s for s in previous.get("songs", [])}
    new_songs = {s["id"]: s for s in effective["songs"]}
    old_charts, new_charts = chart_index(previous), chart_index(current)
    song_fields = ("title", "artist", "bpm", "genre", "version", "locked", "rights", "map")
    chart_fields = (
        "level",
        "level_value",
        "version",
        "notes",
        "note_designer",
        "kanji",
        "description",
        "is_buddy",
    )

    def descriptor(song):
        return {"song_id": song["id"], "title": song["title"]}

    diff = {
        "added_songs": [
            descriptor(new_songs[k]) for k in sorted(new_songs.keys() - old_songs.keys())
        ],
        "missing_songs": [
            descriptor(old_songs[k]) for k in sorted(old_songs.keys() - new_songs.keys())
        ],
        "returned_songs": [
            descriptor(new_songs[k])
            for k in sorted(new_songs.keys() & old_songs.keys())
            if old_songs[k].get("upstream_status") == "missing"
        ],
        "changed_songs": [],
        "added_charts": [],
        "missing_charts": [],
        "returned_charts": [],
        "changed_charts": [],
    }
    for key in sorted(new_songs.keys() & old_songs.keys()):
        changes = {
            f: {"before": old_songs[key].get(f), "after": new_songs[key].get(f)}
            for f in song_fields
            if old_songs[key].get(f) != new_songs[key].get(f)
        }
        if changes:
            diff["changed_songs"].append({**descriptor(new_songs[key]), "changes": changes})
    for key in sorted(new_charts.keys() | old_charts.keys()):
        song, chart = new_charts.get(key, old_charts.get(key))
        row = {
            **descriptor(song),
            "chart_key": key,
            "type": chart["type"],
            "difficulty": chart["difficulty"],
        }
        if key not in old_charts:
            diff["added_charts"].append({**row, "constant": chart["level_value"]})
        elif key not in new_charts:
            diff["missing_charts"].append(row)
        else:
            old = old_charts[key][1]
            changes = {
                f: {"before": old.get(f), "after": chart.get(f)}
                for f in chart_fields
                if old.get(f) != chart.get(f)
            }
            if changes:
                diff["changed_charts"].append({**row, "changes": changes})
            if old.get("upstream_status") == "missing":
                diff["returned_charts"].append(row)
    for song in effective["songs"]:
        song["upstream_status"] = "present"
        for charts in song["difficulties"].values():
            for chart in charts:
                chart["upstream_status"] = "present"
    for key in sorted(old_charts.keys() - new_charts.keys()):
        old_song, old_chart = old_charts[key]
        sid = old_song["id"]
        if sid not in new_songs:
            retained = copy.deepcopy(old_song)
            retained["difficulties"] = {kind: [] for kind in old_song["difficulties"]}
            retained["upstream_status"] = "missing"
            retained["upstream_missing_since"] = old_song.get("upstream_missing_since", timestamp)
            effective["songs"].append(retained)
            new_songs[sid] = retained
        retained_chart = copy.deepcopy(old_chart)
        retained_chart["upstream_status"] = "missing"
        retained_chart["upstream_missing_since"] = old_chart.get(
            "upstream_missing_since", timestamp
        )
        new_songs[sid]["difficulties"].setdefault(old_chart["type"], []).append(retained_chart)
    # Retain a previously known song even when it has no chart rows.
    for sid in sorted(old_songs.keys() - new_songs.keys()):
        retained = copy.deepcopy(old_songs[sid])
        retained.update(
            upstream_status="missing",
            upstream_missing_since=old_songs[sid].get("upstream_missing_since", timestamp),
        )
        effective["songs"].append(retained)
    effective["songs"].sort(key=lambda row: row["id"])
    diff["counts"] = {key: len(value) for key, value in diff.items()}
    diff["counts"]["constant_changes"] = sum(
        "level_value" in row["changes"] for row in diff["changed_charts"]
    )
    return effective, diff


def prepare_update(stage: Path, previous: Path, progress=None) -> dict:
    stage, previous = Path(stage), Path(previous)
    if stage.resolve() == previous.resolve():
        raise ValueError("Prepare updates in a separate staging directory")
    emit = progress or (lambda phase, detail: None)
    started = now()
    warnings, receipts, downloaded = [], [], []
    old_receipts = {r["file"]: r for r in read(previous / "raw/sources.json", [])}
    for filename, url, _key, required in SOURCES:
        emit("sources", {"file": filename, "completed": len(receipts), "total": len(SOURCES)})
        try:
            content, headers = fetch(url)
            value = json.loads(content)
            count = check_source(filename, value)
            receipt = {
                "file": filename,
                "url": url,
                "fetched_at": now(),
                "checked_at": now(),
                "fetch_status": "fresh",
                "headers": headers,
                "record_count": count,
            }
        except Exception as error:
            if required:
                raise RuntimeError(f"落雪数据获取失败（{filename}）：{error}") from error
            previous_path = previous / "raw" / filename
            empty = b'{"songs": []}\n' if filename == "arcade_songs.json" else b"[]\n"
            content = previous_path.read_bytes() if previous_path.exists() else empty
            try:
                count = check_source(filename, json.loads(content), allow_empty=True)
            except Exception:
                content, count = empty, 0
            message = f"{filename}: {type(error).__name__}: {error}"
            warnings.append(
                {"kind": "optional_source_failed", "file": filename, "message": message}
            )
            receipt = {
                **old_receipts.get(filename, {}),
                "file": filename,
                "url": url,
                "checked_at": now(),
                "fetch_status": "stale" if count else "unavailable",
                "error": message,
                "record_count": count,
            }
        receipt.update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
        receipts.append(receipt)
        downloaded.append((filename, content))
    atomic_json(stage / "previous_catalog.json", read(previous / "catalog.json", {"songs": []}))
    for filename, content in downloaded:
        atomic_bytes(stage / "raw" / filename, content)
    atomic_json(stage / "raw/sources.json", receipts)
    current = read(stage / "raw/lxns_songs.json")
    old = read(
        previous / "effective_lxns_songs.json",
        read(previous / "raw/lxns_songs.json", {"songs": []}),
    )
    effective, diff = merge_and_diff(current, old, started)
    old_aliases = {
        r["song_id"]: set(r["aliases"])
        for r in read(previous / "raw/lxns_aliases.json", {"aliases": []})["aliases"]
    }
    new_aliases = {
        r["song_id"]: set(r["aliases"]) for r in read(stage / "raw/lxns_aliases.json")["aliases"]
    }
    diff["changed_aliases"] = [
        {
            "song_id": sid,
            "added": sorted(new_aliases.get(sid, set()) - old_aliases.get(sid, set())),
            "removed": sorted(old_aliases.get(sid, set()) - new_aliases.get(sid, set())),
        }
        for sid in sorted(old_aliases.keys() | new_aliases.keys())
        if old_aliases.get(sid, set()) != new_aliases.get(sid, set())
    ]
    diff["counts"]["changed_aliases"] = len(diff["changed_aliases"])
    current_keys = set(chart_index(current))
    current_ids = {song["id"] for song in current["songs"]}
    retained = {"songs": []}
    for old_song in old.get("songs", []):
        row = copy.deepcopy(old_song)
        row["difficulties"] = {
            kind: [
                chart
                for chart in charts
                if f"{old_song['id']}:{kind}:{chart['difficulty']}" not in current_keys
            ]
            for kind, charts in old_song["difficulties"].items()
        }
        if old_song["id"] not in current_ids or any(row["difficulties"].values()):
            retained["songs"].append(row)
    atomic_json(stage / "retained_lxns_records.json", retained)
    atomic_json(stage / "effective_lxns_songs.json", effective)
    atomic_json(stage / "catalog_diff.json", {"schema_version": 1, "created_at": started, **diff})
    emit("jackets", {"completed": 0, "total": len(effective["songs"])})
    old_images = {
        r["song_id"]: r for r in read(previous / "jacket_manifest.json", {}).get("jackets", [])
    }
    images, next_request = [], 0.0
    for index, song in enumerate(effective["songs"]):
        sid, asset_id = song["id"], song["id"] % 10000
        path = stage / "jackets" / f"{sid}.png"
        row = {
            "song_id": sid,
            "title": song["title"],
            "asset_id": asset_id,
            "file": f"jackets/{sid}.png",
            "url": BASE + f"{asset_id}.png",
        }
        try:
            content = path.read_bytes() if path.is_file() else None
            if content is not None:
                try:
                    metadata = inspect(content)
                except Exception:
                    content = None
            if content is None:
                asset_path = stage / "jackets" / f"{asset_id}.png"
                if asset_path.is_file() and asset_path != path:
                    try:
                        content = asset_path.read_bytes()
                        metadata = inspect(content)
                    except Exception:
                        content = None
                if content is None:
                    time.sleep(max(0.0, next_request - time.monotonic()))
                    next_request = time.monotonic() + 0.35
                    content, headers = fetch(row["url"])
                    metadata = inspect(content)
                    row.update(fetched_at=now(), etag=headers.get("ETag"))
                atomic_bytes(path, content)
            elif old_images.get(sid, {}).get("fetched_at"):
                row["fetched_at"] = old_images[sid]["fetched_at"]
            row.update(metadata, status="ok")
        except Exception as error:
            row.update(status="missing", checked_at=now(), error=f"{type(error).__name__}: {error}")
            warnings.append({"kind": "jacket_unavailable", "song_id": sid, "message": row["error"]})
        images.append(row)
        if (index + 1) % 50 == 0 or index + 1 == len(effective["songs"]):
            emit("jackets", {"completed": index + 1, "total": len(effective["songs"])})
    atomic_json(
        stage / "jacket_manifest.json",
        {
            "schema_version": 1,
            "started_at": started,
            "updated_at": now(),
            "resource_mapping": {
                "rule": "song_id % 10000 (jacket asset only)",
                "source_url": RESOURCE_MAPPING_SOURCE,
            },
            "expected_song_count": len(images),
            "record_count": len(images),
            "ok_count": sum(r["status"] == "ok" for r in images),
            "missing_or_error_count": sum(r["status"] != "ok" for r in images),
            "total_bytes": sum(r.get("bytes", 0) for r in images),
            "jackets": images,
        },
    )
    result = {
        "schema_version": 1,
        "started_at": started,
        "finished_at": now(),
        "sources": receipts,
        "warnings": warnings,
        "diff": diff,
        "counts": {
            "raw_songs": len(current["songs"]),
            "effective_songs": len(effective["songs"]),
            "raw_charts": len(chart_index(current)),
            "effective_charts": len(chart_index(effective)),
            "jackets_missing": sum(r["status"] != "ok" for r in images),
        },
    }
    atomic_json(stage / "pipeline_report.json", result)
    return result
