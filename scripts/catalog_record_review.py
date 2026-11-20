"""Deterministic, record-scoped comparisons and explicit per-field exceptions.

Source IDs stay in their namespaces. CN chart numbers never consume JP levels.
No source file is modified by this module.
"""

from __future__ import annotations

import math
from collections import defaultdict

from .catalog_versions import fingerprint, normalize

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


def _option(source, external_id, path, raw_value, value, identity):
    # The option ID hashes the full source context so a choice names one exact source value.
    proof = fingerprint(
        {
            "source": source,
            "external_id": str(external_id),
            "source_path": path,
            "value": value,
            "raw_value": raw_value,
            "identity": identity,
        }
    )
    return {
        "option_id": source + ":" + proof[:32],
        "source": source,
        "external_id": str(external_id),
        "value": value,
        "source_path": path,
        "evidence_fingerprint": proof,
    }


def _field(song, field_key, field, raw_default, *, chart=None):
    kind = chart["type"] if chart else None
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
    option = _option(
        "lxns", song["id"], path, raw_default, _value(field, raw_default, kind), identity
    )
    return {
        "field_key": field_key,
        "label": LABELS[field],
        "scope": "chart" if chart else "song",
        "chart_key": f"{song['id']}:{kind}:{chart['difficulty']}" if chart else None,
        "type": kind,
        "semantic_domain": "cn_chart" if chart else "shared_metadata",
        "options": [option],
        "reasons": [],
    }


def _add_divingfish(fields, field_key, external_id, path, raw_value, identity):
    field = fields[field_key]
    name = field_key.rsplit(":", 1)[-1] if field["scope"] == "chart" else field_key.split(".")[-1]
    value = _value(name, raw_value, field["type"])
    if value is None:
        return
    option = _option("divingfish", external_id, path, raw_value, value, identity)
    if not any(existing["option_id"] == option["option_id"] for existing in field["options"]):
        field["options"].append(option)


def build_record_review(lxns, fish, version_review=None):
    """Per-song field candidates from LXNS/Diving-Fish values and JP version evidence.

    Only the inputs of the chart compiler are produced: each field's key, label,
    scope/type, semantic domain, distinct source options and version reasons.
    """
    fish_by_id = defaultdict(list)
    for row in fish or []:
        fish_by_id[str(row.get("id"))].append(row)
    version_by_song = defaultdict(list)
    for item in (version_review or {}).get("items", []):
        version_by_song[item["song_id"]].append(item)
    records = []
    for song in lxns.get("songs", []):
        sid = song["id"]
        fields = {
            f"song.{name}": _field(song, f"song.{name}", name, song[name]) for name in SONG_FIELDS
        }
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
                    fields[field_key] = _field(song, field_key, name, original, chart=chart)
            expected = str(sid + 10000 if kind == "dx" else sid)
            expected_type = "SD" if kind == "standard" else "DX"
            fish_matches.extend(
                (kind, row) for row in fish_by_id[expected] if row.get("type") == expected_type
            )
        for kind, row in fish_matches:
            info = row.get("basic_info", {})
            identity = {"title": row.get("title"), "artist": info.get("artist"), "type": kind}
            for name in SONG_FIELDS:
                value = row.get("title") if name == "title" else info.get(name)
                path = "title" if name == "title" else f"basic_info.{name}"
                _add_divingfish(fields, f"song.{name}", row["id"], path, value, identity)
            if kind == "utage":
                continue  # UTAGE level/constant/notes are LXNS-only.
            for chart in song["difficulties"][kind]:
                index = chart["difficulty"]
                key = f"{sid}:{kind}:{index}"
                for name, array in (("level", "level"), ("constant", "ds")):
                    if index < len(row.get(array, [])):
                        _add_divingfish(
                            fields,
                            f"chart:{key}:{name}",
                            row["id"],
                            f"{array}[{index}]",
                            row[array][index],
                            identity,
                        )
                if index < len(row.get("charts", [])):
                    for name, source_name in (("note_designer", "charter"), ("notes", "notes")):
                        _add_divingfish(
                            fields,
                            f"chart:{key}:{name}",
                            row["id"],
                            f"charts[{index}].{source_name}",
                            row["charts"][index].get(source_name),
                            identity,
                        )
        for item in version_by_song[sid]:
            field_key = "version:" + item["object_key"]
            fields[field_key] = {
                "field_key": field_key,
                "label": "日服追加版本 / 日期",
                "scope": item["scope"],
                "type": item["type"],
                "semantic_domain": "jp_release",
                "options": [
                    {
                        "option_id": c["candidate_id"],
                        "source": c["source"],
                        "external_id": c["external_id"],
                        "value": {"version": c["version"], "date": c["date"]},
                        "evidence_fingerprint": c["evidence_fingerprint"],
                    }
                    for c in item["candidates"]
                ],
                "reasons": item["reasons"],
            }
        records.append({"song_id": sid, "fields": list(fields.values())})
    return {"records": records}
