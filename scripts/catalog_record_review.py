"""Deterministic, record-scoped comparisons and explicit per-field exceptions.

Source IDs stay in their namespaces. CN chart numbers never consume JP levels.
No source file is modified by this module.
"""

from __future__ import annotations

import math
from collections import defaultdict
from copy import deepcopy

try:
    from .catalog_versions import (
        fingerprint,
        identity_candidates,
        normalize,
        validate_bound_decision,
    )
except ImportError:
    from catalog_versions import (
        fingerprint,
        identity_candidates,
        normalize,
        validate_bound_decision,
    )


GENRES = {
    "maimai": ("maimai", "舞萌"),
    "POPSアニメ": ("POPSアニメ", "POPS＆アニメ", "流行&动漫"),
    "ゲームバラエティ": ("ゲームバラエティ", "ゲーム＆バラエティ", "其他游戏"),
    "niconicoボーカロイド": (
        "niconicoボーカロイド",
        "niconico＆ボーカロイド",
        "niconico & VOCALOID",
    ),
    "東方Project": ("東方Project", "东方Project"),
    "オンゲキCHUNITHM": ("オンゲキCHUNITHM", "オンゲキ＆CHUNITHM", "音击&中二节奏"),
    "宴会場": ("宴会場",),
}
GENRE_NAMES = {
    normalize(alias): canonical for canonical, aliases in GENRES.items() for alias in aliases
}
LABELS = {
    "title": "曲名",
    "artist": "作者",
    "bpm": "BPM",
    "genre": "分类",
    "level": "等级",
    "constant": "定数",
    "note_designer": "谱师",
    "notes": "音符",
}
SONG_FIELDS = ("title", "artist", "bpm", "genre")
CHART_FIELDS = ("level", "constant", "note_designer", "notes")
NOTE_FIELDS = ("tap", "hold", "slide", "touch", "break")


def _notes(value, kind):
    if isinstance(value, list):
        fields = ("tap", "hold", "slide", "break") if kind == "standard" else NOTE_FIELDS
        if len(value) != len(fields):
            return None
        value = dict(zip(fields, value, strict=False))
        if kind == "standard":
            value["touch"] = 0
        if not all(type(value.get(k)) is int and value[k] >= 0 for k in NOTE_FIELDS):
            return None
        value["total"] = sum(value.values())
    if not isinstance(value, dict):
        return None
    if set(value) == {"left", "right"}:
        parts = {k: _notes(value[k], kind) for k in ("left", "right")}
        return parts if all(parts.values()) else None
    result = {k: value.get(k) for k in (*NOTE_FIELDS, "total")}
    if kind == "standard" and result["touch"] is None:
        result["touch"] = 0
    if not all(type(v) is int and v >= 0 for v in result.values()):
        return None
    return result if result["total"] == sum(result[k] for k in NOTE_FIELDS) else None


def _value(field, value, kind=None):
    if field == "genre":
        return (
            GENRE_NAMES.get(normalize(value), value)
            if isinstance(value, str) and value.strip()
            else None
        )
    if field == "note_designer":
        return (
            value if isinstance(value, str) and value.strip() not in ("", "-", "－", "—") else None
        )
    if field in ("bpm", "constant"):
        return (
            float(value)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0
            else None
        )
    if field == "notes":
        return _notes(value, kind)
    return value if isinstance(value, str) and value.strip() else None


def _object_fingerprint(song, field_key):
    return fingerprint(
        {
            "song_id": song["id"],
            "title": song["title"],
            "artist": song["artist"],
            "field_key": field_key,
        }
    )


def _option(source, external_id, path, raw_value, value, identity, *, match="matched"):
    evidence = {
        "source": source,
        "external_id": str(external_id),
        "source_path": path,
        "value": value,
        "raw_value": raw_value,
        "identity": identity,
    }
    proof = fingerprint(evidence)
    return {
        "option_id": source + ":" + proof[:32],
        "source": source,
        "external_id": str(external_id),
        "value": deepcopy(value),
        "raw_value": deepcopy(raw_value),
        "source_path": path,
        "evidence_fingerprint": proof,
        "identity_match": match,
        "selectable": True,
    }


def _field(song, field_key, field, raw_default, scope, *, chart=None):
    kind = chart["type"] if chart else None
    normalized = _value(field, raw_default, kind)
    original_field = {"constant": "level_value"}.get(field, field)
    path = (
        f"difficulties.{kind}[{chart['difficulty']}].{original_field}" if chart else original_field
    )
    identity = {
        "song_id": song["id"],
        "title": song["title"],
        "artist": song["artist"],
        "type": kind,
        "difficulty_index": chart["difficulty"] if chart else None,
    }
    # LXNS remains selectable even if its canonical absence is represented by an empty string.
    option = _option("lxns", song["id"], path, raw_default, normalized, identity)
    return {
        "field_key": field_key,
        "label": LABELS[field],
        "scope": scope,
        "chart_key": f"{song['id']}:{kind}:{chart['difficulty']}" if chart else None,
        "type": kind,
        "difficulty_index": chart["difficulty"] if chart else None,
        "semantic_domain": "cn_chart" if chart else "shared_metadata",
        "object_key": f"field:{song['id']}:{field_key}",
        "object_fingerprint": _object_fingerprint(song, field_key),
        "current": {"value": deepcopy(raw_default), "source": "lxns", "status": "resolved"},
        "automatic": {"value": deepcopy(raw_default), "source": "lxns"},
        "options": [option],
        "source_context_fingerprints": [option["evidence_fingerprint"]],
        "current_option_id": option["option_id"],
        "recommended_option_id": option["option_id"],
        "selectable": True,
        "status": "resolved",
        "has_difference": False,
        "decision": None,
        "reasons": [],
    }


def _arcade_record_membership(song, rows, version_items):
    """Choose identities per chart format, retaining excluded raw rows for audit."""
    included = defaultdict(dict)
    version_items = {
        item["type"]: item for item in version_items if item["scope"] == "ordinary_format"
    }
    for kind in ("standard", "dx", "utage"):
        if not song["difficulties"].get(kind):
            continue
        source_type = "std" if kind == "standard" else kind
        typed_rows = [
            r for r in rows if any(s.get("type") == source_type for s in r.get("sheets", []))
        ]
        item = version_items.get(kind)
        identity = None
        if item:
            decisions = [d for d in item["decisions"] if d["kind"] == "identity"]
            if len(decisions) == 1:
                try:
                    identity = validate_bound_decision(item, decisions[0])
                except ValueError:
                    pass
            _, eligible, exact = identity_candidates(
                item["candidates"], song["title"], song["artist"], identity
            )
            exact_ids = {c["external_id"] for c in exact if c["source"] == "arcade_songs"}
            if exact_ids:
                chosen = exact_ids
                match = (
                    "identity_selected"
                    if identity and identity["action"] == "select"
                    else "matched"
                    if len(exact_ids) == 1
                    else "identity_ambiguous"
                )
            else:
                chosen = {c["external_id"] for c in eligible if c["source"] == "arcade_songs"}
                match = "identity_ambiguous" if len(exact_ids) > 1 else "artist_mismatch"
        else:
            exact = [r for r in typed_rows if normalize(r["artist"]) == normalize(song["artist"])]
            chosen = {r["songId"] for r in exact} or {r["songId"] for r in typed_rows}
            match = (
                "matched"
                if len(exact) == 1
                else "identity_ambiguous"
                if len(exact) > 1
                else "artist_mismatch"
            )
        for row in typed_rows:
            if row["songId"] in chosen:
                included[row["songId"]][kind] = match
    return included


def build_record_review(
    lxns,
    fish,
    arcade,
    version_review=None,
    adjudications=None,
):
    """Compare complete local song records; retain distinct region/scope meanings."""
    fish_by_id, arcade_by_title = defaultdict(list), defaultdict(list)
    for row in fish or []:
        fish_by_id[str(row.get("id"))].append(row)
    for row in (arcade or {}).get("songs", []):
        arcade_by_title[normalize(row.get("title", ""))].append(row)
    version_by_song = defaultdict(list)
    for item in (version_review or {}).get("items", []):
        version_by_song[item["song_id"]].append(deepcopy(item))
    records = []
    for song in lxns.get("songs", []):
        sid = song["id"]
        fields = {
            f"song.{name}": _field(song, f"song.{name}", name, song[name], "song")
            for name in SONG_FIELDS
        }
        source_records = [
            {
                "source": "lxns",
                "external_id": str(sid),
                "record": deepcopy(song),
                "identity_match": "authority",
            }
        ]
        fish_matches = []
        for kind, charts in song["difficulties"].items():
            if not charts:
                continue
            for chart in charts:
                key = f"{sid}:{kind}:{chart['difficulty']}"
                for name in CHART_FIELDS:
                    original = chart["level_value"] if name == "constant" else chart[name]
                    if name == "constant" and kind == "utage" and original == 0:
                        original = None
                    field_key = f"chart:{key}:{name}"
                    fields[field_key] = _field(
                        song, field_key, name, original, "chart", chart=chart
                    )
            expected = str(sid + 10000 if kind == "dx" else sid)
            expected_type = "SD" if kind == "standard" else "DX"
            for row in fish_by_id[expected]:
                if row.get("type") == expected_type:
                    fish_matches.append((kind, row))
                    source_records.append(
                        {
                            "source": "divingfish",
                            "external_id": str(row["id"]),
                            "type": kind,
                            "identity_match": "id_and_type",
                            "record": deepcopy(row),
                        }
                    )
        same_title = arcade_by_title[normalize(song["title"])]
        membership = _arcade_record_membership(song, same_title, version_by_song[sid])
        for row in same_title:
            kinds = {
                "standard" if s.get("type") == "std" else s.get("type")
                for s in row.get("sheets", [])
            }
            if not any(song["difficulties"].get(kind) for kind in kinds):
                continue
            selected_types = membership[row["songId"]]
            match = next(iter(selected_types.values()), "excluded_same_title")
            source_records.append(
                {
                    "source": "arcade_songs",
                    "external_id": row["songId"],
                    "identity_match": match,
                    "comparison_eligible": bool(selected_types),
                    "comparison_types": sorted(selected_types),
                    "record": deepcopy(row),
                }
            )

        def add(
            field_key,
            source,
            external_id,
            path,
            raw_value,
            identity,
            match="matched",
            *,
            target_fields=fields,
        ):
            field = target_fields[field_key]
            name = (
                field_key.rsplit(":", 1)[-1]
                if field["scope"] == "chart"
                else field_key.split(".")[-1]
            )
            value = _value(name, raw_value, field["type"])
            if value is None:
                return
            option = _option(source, external_id, path, raw_value, value, identity, match=match)
            field["source_context_fingerprints"].append(option["evidence_fingerprint"])
            if not any(
                existing["option_id"] == option["option_id"] for existing in field["options"]
            ):
                field["options"].append(option)

        for kind, row in fish_matches:
            info = row.get("basic_info", {})
            identity = {"title": row.get("title"), "artist": info.get("artist"), "type": kind}
            for name in SONG_FIELDS:
                value = row.get("title") if name == "title" else info.get(name)
                add(
                    f"song.{name}",
                    "divingfish",
                    row["id"],
                    "title" if name == "title" else f"basic_info.{name}",
                    value,
                    identity,
                    "id_and_type",
                )
            for chart in song["difficulties"][kind]:
                index = chart["difficulty"]
                key = f"{sid}:{kind}:{index}"
                for name, array in (("level", "level"), ("constant", "ds")):
                    if kind != "utage" and index < len(row.get(array, [])):
                        add(
                            f"chart:{key}:{name}",
                            "divingfish",
                            row["id"],
                            f"{array}[{index}]",
                            row[array][index],
                            identity,
                            "id_and_type",
                        )
                if kind != "utage" and index < len(row.get("charts", [])):
                    for name, source_name in (("note_designer", "charter"), ("notes", "notes")):
                        add(
                            f"chart:{key}:{name}",
                            "divingfish",
                            row["id"],
                            f"charts[{index}].{source_name}",
                            row["charts"][index].get(source_name),
                            identity,
                            "id_and_type",
                        )
        for field in fields.values():
            field["source_context_fingerprint"] = fingerprint(
                sorted(set(field.pop("source_context_fingerprints")))
            )
            values = {fingerprint(option["value"]) for option in field["options"]}
            field["has_difference"] = len(values) > 1
            field["status"] = "pending" if field["has_difference"] else "resolved"
            field["current"]["status"] = field["status"]
        identities = version_by_song[sid]
        for item in identities:
            object_key = item["object_key"]
            fields["version:" + object_key] = {
                "field_key": "version:" + object_key,
                "label": "日服追加版本 / 日期",
                "scope": item["scope"],
                "chart_key": object_key if item["scope"] == "chart" else None,
                "type": item["type"],
                "difficulty_index": item["difficulty_index"],
                "semantic_domain": "jp_release",
                "version_object_key": object_key,
                "object_fingerprint": item["object_fingerprint"],
                "current": {
                    "value": {
                        "version": item["current"]["version"],
                        "date": item["current"]["date"],
                    },
                    "source": item["current"]["provider"],
                    "status": item["current"]["status"],
                },
                "options": [
                    {
                        "option_id": c["candidate_id"],
                        "candidate_id": c["candidate_id"],
                        "source": c["source"],
                        "external_id": c["external_id"],
                        "value": {"version": c["version"], "date": c["date"]},
                        "evidence_fingerprint": c["evidence_fingerprint"],
                        "selectable": bool(c["version"]),
                    }
                    for c in item["candidates"]
                ],
                "selectable": True,
                "status": item["status"],
                "has_difference": item["status"] in ("pending", "conflict"),
                "decision": next((d for d in item["decisions"] if d["kind"] == "version"), None),
                "reasons": item["reasons"],
            }
            field = fields["version:" + object_key]
            matching = [o for o in field["options"] if o["value"] == field["current"]["value"]]
            field["current_option_id"] = matching[0]["option_id"] if len(matching) == 1 else None
            field["recommended_option_id"] = (
                field["current_option_id"] if item["status"] == "resolved" else None
            )
        records.append(
            {
                "song_id": sid,
                "title": song["title"],
                "artist": song["artist"],
                "status": "resolved",
                "source_identity": {k: song[k] for k in ("id", "title", "artist")},
                "fields": list(fields.values()),
                "identities": identities,
                "source_records": source_records,
            }
        )
    return {
        "schema_version": 1,
        "decision_revision": (adjudications or {}).get("revision", 0),
        "records": records,
    }
