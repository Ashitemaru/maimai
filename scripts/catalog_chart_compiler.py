"""Schema 2: chart admission, durable user choices, and flat catalog storage.

The legacy evidence adapters remain internal. Only admitted charts are published.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import date
from pathlib import Path

from .catalog_pipeline import atomic_json as write
from .catalog_policy import source_policy
from .catalog_record_review import build_record_review
from .catalog_versions import empty_release, fingerprint, resolve_field_versions

FIELDS = {
    "title",
    "artist",
    "bpm",
    "genre",
    "level",
    "constant",
    "note_designer",
    "notes",
    "jp_release_version",
}


def read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else deepcopy(default)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def override_index(path):
    """Index remy_release_overrides.json ({"overrides": [...]}) by chart_key."""
    document = read(path, {"overrides": []})
    rows = document.get("overrides") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return {}, [{"kind": "invalid_release_override_file"}]
    result, errors = {}, []
    for record in rows:
        if not isinstance(record, dict):
            errors.append({"kind": "invalid_release_override", "record": record})
            continue
        chart_key = record.get("chart_key")
        if not chart_key:
            errors.append({"kind": "invalid_release_override", "record": record})
        elif chart_key in result:
            errors.append({"kind": "duplicate_release_override", "chart_key": chart_key})
        else:
            result[chart_key] = record
    return result, errors


def canonical_id(song_id, kind):
    return int(song_id) + 10000 if kind == "dx" else int(song_id)


def chart_mappings(lxns):
    rows, keys = [], set()
    for song in lxns["songs"]:
        for kind, charts in song["difficulties"].items():
            for chart in charts:
                ident, difficulty = canonical_id(song["id"], kind), chart["difficulty"]
                key = f"{ident}:{difficulty}"
                if key in keys:
                    raise ValueError(f"Canonical chart collision: {key}")
                keys.add(key)
                rows.append(
                    {
                        "id": ident,
                        "difficulty": difficulty,
                        "chart_key": key,
                        "legacy_chart_key": f"{song['id']}:{kind}:{difficulty}",
                        "lxns_song_id": song["id"],
                        "type": kind,
                    }
                )
    return {"schema_version": 2, "mappings": sorted(rows, key=lambda r: (r["id"], r["difficulty"]))}


def _durable(mapping, field, value, source, **extra):
    return {
        "object_key": f"chart:{mapping['id']}:{mapping['difficulty']}:{field}",
        "kind": "chart_field",
        "id": mapping["id"],
        "difficulty": mapping["difficulty"],
        "field_key": field,
        "action": "select",
        "value": deepcopy(value),
        "source": source,
        **deepcopy(extra),
    }


def validate_chart_decisions(review, ident, difficulty, choices):
    if type(ident) is not int or type(difficulty) is not int or not isinstance(choices, list):
        raise ValueError("谱面或字段选择格式无效")
    record = next(
        (
            r
            for r in review.get("records", [])
            if r["id"] == ident and r["difficulty"] == difficulty
        ),
        None,
    )
    if not record or record.get("unavailable"):
        raise ValueError("待入库谱面不存在")
    if record.get("decision_pending"):
        raise ValueError("该谱面已保存裁决，正在等待发布，请重试应用")
    fields = {f["field_key"]: f for f in record["fields"] if f.get("required")}
    if not fields:
        raise ValueError("该谱面没有待裁决字段")
    selected = {}
    for choice in choices:
        if (
            not isinstance(choice, dict)
            or choice.get("field_key") not in fields
            or choice["field_key"] in selected
        ):
            raise ValueError("字段重复或不属于该谱面的待裁决字段")
        selected[choice["field_key"]] = choice
    if selected.keys() != fields.keys():
        raise ValueError("请一次选择此谱面全部待裁决字段")
    result = []
    for field, choice in selected.items():
        option = next(
            (o for o in fields[field]["options"] if o["option_id"] == choice.get("option_id")), None
        )
        if option is None:
            raise ValueError(f"字段来源证据不存在：{field}")
        if field == "jp_release_version" and option.get("date"):
            date.fromisoformat(option["date"])
        result.append(
            _durable(
                record,
                field,
                option["value"],
                option["source"],
                option_id=option["option_id"],
                external_id=option.get("external_id"),
                evidence_fingerprint=option.get("evidence_fingerprint"),
                evidence=deepcopy(option.get("evidence")),
                date=option.get("date") if field == "jp_release_version" else None,
            )
        )
    return result


def _decision_document(directory, explicit=None):
    document = (
        explicit
        if explicit is not None
        else read(Path(directory) / "catalog_adjudications.json", {"revision": 0, "decisions": []})
    )
    if any(d.get("kind") != "chart_field" for d in document.get("decisions", [])):
        raise ValueError("裁决不是新版格式，无法编译曲库")
    return {**deepcopy(document), "schema_version": 2}


def compile_charts(lxns, legacy_catalog, legacy_review, document):
    """Pure admission result from normalized adapters and permanent selected values."""
    mappings = chart_mappings(lxns)
    by_legacy = {m["legacy_chart_key"]: m for m in mappings["mappings"]}
    record_by_song = {r["song_id"]: r for r in legacy_review["records"]}
    decisions = defaultdict(dict)
    for decision in document.get("decisions", []):
        if decision.get("kind") != "chart_field" or decision.get("field_key") not in FIELDS:
            raise ValueError("Invalid durable chart-field decision")
        if (
            type(decision.get("id")) is not int
            or type(decision.get("difficulty")) is not int
            or decision.get("action") != "select"
            or "value" not in decision
        ):
            raise ValueError("Malformed durable chart-field decision")
        key = f"{decision['id']}:{decision['difficulty']}"
        if decision.get("object_key") != f"chart:{key}:{decision['field_key']}":
            raise ValueError("Durable decision identity mismatch")
        if decision["field_key"] in decisions[key]:
            raise ValueError(f"Duplicate durable chart-field decision: {key}")
        decisions[key][decision["field_key"]] = decision
    formal, review_records = [], []
    remy_counts = {"total": 0, "verified": 0, "pending": 0}
    for song in legacy_catalog["songs"]:
        record = record_by_song[song["id"]]
        for old in song["charts"]:
            mapping = by_legacy[old["chart_key"]]
            key, index = mapping["chart_key"], mapping["difficulty"]
            flat = {
                **deepcopy(
                    {
                        k: v
                        for k, v in song.items()
                        if k not in ("id", "charts", "api_version", "external_ids")
                    }
                ),
                **deepcopy(old),
                **mapping,
                "difficulty_index": index,
                "cn_version": old["api_version"],
                "api_version": old["api_version"],
                "external_ids": [
                    deepcopy(e)
                    for e in song["external_ids"]
                    if e.get("type") in (None, old["type"])
                ],
                "source_ids": {
                    "lxns": {"song_id": song["id"], "type": old["type"], "difficulty": index},
                    "divingfish": {"id": old["divingfish_id"]}
                    if old.get("divingfish_id")
                    else None,
                    "legacy_chart_key": old["chart_key"],
                },
                "field_decisions": deepcopy(decisions[key]),
            }
            # Raw aliases stay in source snapshots; the published chart has one
            # value for each business field even after a permanent user choice.
            flat.pop("level_value", None)
            flat.pop("version", None)
            conflicts = []
            for source_field in record["fields"]:
                field_key = source_field["field_key"]
                is_jp = source_field["semantic_domain"] == "jp_release"
                if is_jp:
                    matches = source_field["type"] == old["type"] and (
                        index == 4 if source_field["scope"] == "chart" else index < 4
                    )
                    field = "jp_release_version"
                elif source_field["scope"] == "song":
                    matches, field = True, field_key.split(".")[-1]
                else:
                    matches, field = (
                        source_field.get("chart_key") == old["chart_key"],
                        field_key.rsplit(":", 1)[-1],
                    )
                if not matches or field in decisions[key]:
                    continue
                options = []
                allowed_sources = (
                    ({"remywiki"} if index == 4 else {"arcade_songs"})
                    if is_jp
                    else {"lxns", "divingfish"}
                )
                for option in source_field["options"]:
                    if option["source"] not in allowed_sources:
                        continue
                    value = option["value"]["version"] if is_jp else option["value"]
                    if value is None or is_jp and not value:
                        continue
                    adapted = deepcopy(option)
                    adapted["value"] = deepcopy(value)
                    if is_jp:
                        adapted["date"] = option["value"]["date"]
                    options.append(adapted)
                values = {
                    fingerprint({"value": o["value"], "date": o.get("date")}) for o in options
                }
                unknown_version = is_jp and "unknown_jp_version" in source_field.get("reasons", [])
                if len(values) <= 1 and not (unknown_version and options):
                    if options and not is_jp and not any(o["source"] == "lxns" for o in options):
                        supplied = options[0]
                        flat[field] = deepcopy(supplied["value"])
                        flat.setdefault("automatic_field_sources", {})[field] = {
                            key: deepcopy(supplied.get(key))
                            for key in (
                                "source",
                                "external_id",
                                "source_path",
                                "evidence_fingerprint",
                            )
                        }
                        if field == "constant":
                            flat["constant_source"] = supplied["source"] + "_current_cn"
                    continue
                conflicts.append(
                    {
                        "field_key": field,
                        "label": source_field["label"],
                        "required": True,
                        "options": options,
                        "reasons": deepcopy(source_field.get("reasons", [])),
                    }
                )
            for field, decision in decisions[key].items():
                flat[field] = deepcopy(decision["value"])
                if field == "constant":
                    flat["constant_source"] = "manual_" + decision["source"]
                if field == "jp_release_version":
                    flat.update(
                        jp_release_date=decision.get("date"),
                        jp_release_provider="manual",
                        release_status="manual",
                        jp_release_source=decision.get("source"),
                        jp_release_evidence=json.dumps(
                            decision.get("evidence"), ensure_ascii=False
                        ),
                        jp_release_provenance={"durable_decision": deepcopy(decision)},
                        release_source_scope="chart",
                    )
            flat["version_pending"] = (
                flat["jp_release_version"] is None and "jp_release_version" not in decisions[key]
            )
            flat["admission_status"] = "pending" if conflicts else "admitted"
            if old["type"] in ("standard", "dx") and index == 4:
                remy_counts["total"] += 1
                verified = bool(flat["jp_release_version"]) and flat["release_status"] in (
                    "verified",
                    "manual",
                )
                remy_counts["verified" if verified else "pending"] += 1
            review_records.append(
                {
                    **mapping,
                    "title": flat["title"],
                    "type": old["type"],
                    "difficulty_name": old["difficulty_name"],
                    "status": "pending" if conflicts else "resolved",
                    "fields": conflicts,
                    "decisions": list(deepcopy(decisions[key]).values()),
                }
            )
            if not conflicts:
                formal.append(flat)
    known = {r["chart_key"] for r in review_records}
    for key, fields in decisions.items():
        if fields and key not in known:
            sample = next(iter(fields.values()))
            review_records.append(
                {
                    "id": sample["id"],
                    "difficulty": sample["difficulty"],
                    "chart_key": key,
                    "title": f"ID {sample['id']}",
                    "type": None,
                    "difficulty_name": str(sample["difficulty"]),
                    "status": "resolved",
                    "fields": [],
                    "unavailable": True,
                    "decisions": list(deepcopy(fields).values()),
                }
            )
    review_records.sort(key=lambda r: (r["id"], r["difficulty"]))
    formal.sort(key=lambda r: (r["id"], r["difficulty"]))
    counts = {
        "total": len(mappings["mappings"]),
        "admitted": len(formal),
        "pending": len(mappings["mappings"]) - len(formal),
    }
    return {
        "charts": formal,
        "review": {
            "schema_version": 2,
            "decision_revision": document.get("revision", 0),
            "counts": counts,
            "remy_counts": remy_counts,
            "records": review_records,
        },
        "mappings": mappings,
    }


def _load_evidence(directory, document):
    directory = Path(directory)
    lxns = read(directory / "effective_lxns_songs.json", read(directory / "raw/lxns_songs.json"))
    if not isinstance(lxns, dict):
        raise ValueError("Catalog build failed: LXNS song list is missing")
    arcade = read(directory / "raw/arcade_songs.json", {"songs": []})
    overrides, errors = override_index(directory / "remy_release_overrides.json")
    if errors:
        raise ValueError("Invalid Remy evidence index")
    versions = resolve_field_versions(
        lxns,
        arcade,
        list(overrides.values()),
        read(directory / "remy_conflicts.json"),
        document.get("revision", 0),
    )
    review = build_record_review(
        lxns, read(directory / "raw/divingfish_music_data.json", []), version_review=versions
    )
    return lxns, versions, review


def _base_legacy_catalog(directory, lxns, versions):
    """Small pure source projection; no admission state or display overrides reused."""
    directory = Path(directory)
    aliases = defaultdict(set)
    for row in read(directory / "raw/lxns_aliases.json", {"aliases": []})["aliases"]:
        aliases[row["song_id"]].update(row["aliases"])
    jackets = {
        row["song_id"]: row
        for row in read(directory / "jacket_manifest.json", {"jackets": []})["jackets"]
    }
    fish = defaultdict(list)
    for row in read(directory / "raw/divingfish_music_data.json", []):
        fish[(str(row["id"]), row["type"])].append(row)
    result = []
    for raw in lxns["songs"]:
        song = {k: deepcopy(v) for k, v in raw.items() if k != "difficulties"}
        sid = raw["id"]
        song.update(
            source_identity={k: raw[k] for k in ("id", "title", "artist")},
            aliases=sorted(aliases[sid]),
            jacket_path=f"jackets/{sid}.png",
            jacket_available=jackets.get(sid, {}).get("status") == "ok",
            jacket_source=deepcopy(jackets.get(sid)),
            api_version=raw["version"],
            external_ids=[{"namespace": "lxns", "id": str(sid), "type": None}],
            charts=[],
        )
        song.setdefault("locked", False)
        song.setdefault("rights", None)
        song.setdefault("map", None)
        for kind, charts in raw["difficulties"].items():
            external_id = str(canonical_id(sid, kind))
            matching = fish[(external_id, "SD" if kind == "standard" else "DX")]
            ext = matching[0] if len(matching) == 1 else None
            rule = {
                "standard": "same_id_standard",
                "dx": "lxns_id_plus_10000",
                "utage": "same_id_utage_no_offset",
            }[kind]
            if ext and charts:
                song["external_ids"].append(
                    {
                        "namespace": "divingfish",
                        "id": external_id,
                        "type": kind,
                        "mapping_rule": rule,
                        "source_title": ext["title"],
                    }
                )
            for chart in charts:
                index = chart["difficulty"]
                old_key = f"{sid}:{kind}:{index}"
                release_key = f"{sid}:{kind}:ordinary" if kind != "utage" and index < 4 else old_key
                song["charts"].append(
                    {
                        **deepcopy(chart),
                        "chart_key": old_key,
                        "difficulty_index": index,
                        "difficulty_name": "UTAGE"
                        if kind == "utage"
                        else ("BASIC", "ADVANCED", "EXPERT", "MASTER", "Re:MASTER")[index],
                        "constant": None
                        if kind == "utage" and chart["level_value"] == 0
                        else chart["level_value"],
                        "constant_source": "lxns_current_cn",
                        "api_version": chart["version"],
                        "divingfish_id": external_id if ext else None,
                        "id_mapping_rule": rule if ext else None,
                        **deepcopy(versions["objects"].get(release_key, empty_release())),
                    }
                )
        result.append(song)
    return {"songs": result}


def calculate(directory, document=None):
    document = _decision_document(directory, document)
    lxns, versions, review = _load_evidence(directory, document)
    result = compile_charts(lxns, _base_legacy_catalog(directory, lxns, versions), review, document)
    return {**result, "lxns": lxns, "versions": versions, "document": document}


def load_record_review(directory, adjudications=None):
    """Return pending admission evidence; there is deliberately no current/default."""
    directory = Path(directory)
    published_document = _decision_document(directory)
    document = (
        _decision_document(directory, adjudications)
        if adjudications is not None
        else published_document
    )
    saved = read(directory / "catalog_record_review.json")
    review = (
        saved
        if saved and saved.get("schema_version") == 2
        else calculate(directory, published_document)["review"]
    )
    review["applied_decision_revision"] = published_document.get("revision", 0)
    review["decision_revision"] = document.get("revision", 0)
    before = {
        (d["id"], d["difficulty"], d["field_key"]): d
        for d in published_document.get("decisions", [])
    }
    after = {(d["id"], d["difficulty"], d["field_key"]): d for d in document.get("decisions", [])}
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    for record in review["records"]:
        keys = [key for key in changed if key[:2] == (record["id"], record["difficulty"])]
        if keys:
            record["decision_pending"] = True
            record["pending_decisions"] = [deepcopy(after[key]) for key in keys if key in after]
            record["pending_revocations"] = [key[2] for key in keys if key not in after]
    return review


SQL_METADATA = ("versions_cn", "snapshot_at")


def sql_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_sqlite(path, catalog):
    """Runtime index: the app reads charts.data_json plus two metadata keys."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(temporary)) as conn, conn:
            conn.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE charts (id INTEGER NOT NULL, difficulty INTEGER NOT NULL,
                    type TEXT NOT NULL, data_json TEXT NOT NULL, PRIMARY KEY(id, difficulty));
            """)
            conn.executemany(
                "INSERT INTO metadata VALUES (?,?)",
                [(key, sql_text(catalog[key])) for key in SQL_METADATA],
            )
            conn.executemany(
                "INSERT INTO charts VALUES (?,?,?,?)",
                [(c["id"], c["difficulty"], c["type"], sql_text(c)) for c in catalog["charts"]],
            )
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def build(directory):
    directory = Path(directory)
    result = calculate(directory)
    write(directory / "catalog_record_review.json", result["review"])
    write(
        directory / "pending_charts.json",
        {
            "schema_version": 2,
            "counts": result["review"]["counts"],
            "records": [r for r in result["review"]["records"] if r["status"] == "pending"],
        },
    )
    write(
        directory / "catalog_review.json",
        {k: v for k, v in result["versions"].items() if k != "objects"},
    )
    write(directory / "chart_key_map.json", result["mappings"])
    inputs = []
    names = [
        *sorted((directory / "raw").glob("*.json")),
        *[
            directory / n
            for n in (
                "effective_lxns_songs.json",
                "retained_lxns_records.json",
                "catalog_diff.json",
                "pipeline_report.json",
                "catalog_adjudications.json",
                "remy_release_overrides.json",
                "remy_conflicts.json",
                "jacket_manifest.json",
                "catalog_review.json",
                "catalog_record_review.json",
                "pending_charts.json",
                "chart_key_map.json",
            )
        ],
    ]
    for path in names:
        if path.is_file():
            inputs.append(
                {
                    "path": str(path.relative_to(directory)),
                    "sha256": digest(path),
                    "bytes": path.stat().st_size,
                }
            )
    source_receipts = read(directory / "raw/sources.json", [])
    counts = {
        **result["review"]["counts"],
        "charts": len(result["charts"]),
        "version_pending": sum(c["version_pending"] for c in result["charts"]),
        "jackets_missing": sum(not c["jacket_available"] for c in result["charts"]),
    }
    catalog = {
        "schema_version": 2,
        "primary_source": "lxns",
        "data_region": "CN",
        "source_policy": source_policy(),
        "decision_revision": result["document"].get("revision", 0),
        "snapshot_at": max((s.get("fetched_at", "") for s in source_receipts), default=None),
        "counts": counts,
        "sources": source_receipts,
        "input_files": inputs,
        "versions_cn": result["lxns"]["versions"],
        "genres": result["lxns"]["genres"],
        "charts": result["charts"],
    }
    write(directory / "catalog.json", catalog)
    write_sqlite(directory / "catalog.sqlite", catalog)
    report = {
        "schema_version": 2,
        "status": "ok",
        "source_policy": source_policy(),
        "decision_revision": catalog["decision_revision"],
        "counts": counts,
        "inputs": inputs,
        "issues": [
            {
                "kind": "chart_pending_admission",
                "chart_key": r["chart_key"],
                "fields": [f["field_key"] for f in r["fields"]],
            }
            for r in result["review"]["records"]
            if r["status"] == "pending"
        ],
        "outputs": [
            {
                "path": name,
                "sha256": digest(directory / name),
                "bytes": (directory / name).stat().st_size,
            }
            for name in (
                "catalog.json",
                "catalog.sqlite",
                "catalog_record_review.json",
                "pending_charts.json",
                "chart_key_map.json",
            )
        ],
    }
    write(directory / "build_report.json", report)
    return report
