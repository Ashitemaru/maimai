"""Schema 2: chart admission, durable user choices, and flat catalog storage.

The legacy evidence adapters remain internal. Only admitted charts are published.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import date
from pathlib import Path

try:
    from .catalog_policy import source_policy
    from .catalog_versions import fingerprint
except ImportError:
    from catalog_policy import source_policy
    from catalog_versions import fingerprint

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


def write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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


def _legacy_charts(catalog):
    if (catalog or {}).get("schema_version") == 2:
        return {c["legacy_chart_key"]: c for c in catalog.get("charts", [])}
    return {
        chart["chart_key"]: {**chart, "lxns_song_id": song["id"]}
        for song in (catalog or {}).get("songs", [])
        for chart in song.get("charts", [])
    }


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


def migrate_decisions(document, directory):
    """Pure conversion. Missing selected evidence aborts rather than losing intent."""
    document = deepcopy(document or {"revision": 0, "decisions": []})
    directory = Path(directory)
    lxns = read(directory / "effective_lxns_songs.json", read(directory / "raw/lxns_songs.json"))
    mappings = chart_mappings(lxns)["mappings"]
    catalog = read(directory / "catalog.json", {})
    previous = read(directory / "previous_catalog.json", {})
    charts = {**_legacy_charts(previous), **_legacy_charts(catalog)}
    old_review = read(directory / "catalog_review.json", {"items": []})
    items = {r["object_key"]: r for r in old_review.get("items", [])}
    output, converted = {}, []

    def add(result, original):
        key = result["object_key"]
        if key in output and (output[key]["value"], output[key].get("date")) != (
            result["value"],
            result.get("date"),
        ):
            raise ValueError(f"旧裁决迁移存在不同值，请先处理：{key}")
        if key not in output or original.get("kind") != "identity":
            output[key] = result

    for decision in document.get("decisions", []):
        if decision.get("kind") == "chart_field":
            add(deepcopy(decision), decision)
            continue
        kind, key = decision.get("kind"), decision.get("object_key")
        if kind == "field":
            field_key = decision.get("field_key", "")
            field = (
                field_key.rsplit(".", 1)[-1]
                if field_key.startswith("song.")
                else field_key.rsplit(":", 1)[-1]
            )
            targets = [
                m
                for m in mappings
                if m["lxns_song_id"] == decision.get("song_id")
                and (
                    field_key.startswith("song.")
                    or field_key == f"chart:{m['legacy_chart_key']}:{field}"
                )
            ]
            if field not in FIELDS or "value" not in decision or not targets:
                raise ValueError(f"无法恢复旧字段裁决：{key}")
            for mapping in targets:
                add(
                    _durable(
                        mapping,
                        field,
                        decision["value"],
                        decision.get("source", "manual"),
                        migrated_from=deepcopy(decision),
                        evidence_fingerprint=decision.get("evidence_fingerprint"),
                        option_id=decision.get("option_id"),
                        note=decision.get("note", ""),
                    ),
                    decision,
                )
        elif kind in ("identity", "version"):
            targets = [
                m
                for m in mappings
                if key == m["legacy_chart_key"]
                or key == f"{m['lxns_song_id']}:{m['type']}:ordinary"
                and m["difficulty"] < 4
            ]
            if not targets:
                raise ValueError(f"无法对应旧版本裁决：{key}")
            candidate = next(
                (
                    c
                    for c in items.get(key, {}).get("candidates", [])
                    if c["candidate_id"] == decision.get("candidate_id")
                ),
                None,
            )
            if candidate is None:
                for mapping in targets:
                    proof = (
                        charts.get(mapping["legacy_chart_key"], {}).get("jp_release_provenance")
                        or {}
                    ).get("candidate") or {}
                    if proof.get("candidate_id") == decision.get("candidate_id") and proof.get(
                        "version"
                    ):
                        candidate = proof
                        break
            for mapping in targets:
                if decision["action"] in ("set", "keep") and decision.get("version"):
                    value, day = decision["version"], decision.get("date")
                elif decision["action"] == "select" and candidate and candidate.get("version"):
                    value, day = candidate["version"], candidate.get("date")
                elif decision["action"] == "reject" and mapping["legacy_chart_key"] in charts:
                    chosen = charts[mapping["legacy_chart_key"]]
                    value, day = chosen.get("jp_release_version"), chosen.get("jp_release_date")
                    if not value:
                        raise ValueError(f"旧拒绝没有已选择的版本值，不能迁移为空版本裁决：{key}")
                else:
                    raise ValueError(f"无法恢复旧版本选择的值/证据：{key}")
                add(
                    _durable(
                        mapping,
                        "jp_release_version",
                        value,
                        candidate.get("source", "manual") if candidate else "manual",
                        date=day,
                        migrated_from=deepcopy(decision),
                        evidence=deepcopy(candidate),
                        legacy_identity_decision=deepcopy(decision) if kind == "identity" else None,
                    ),
                    decision,
                )
        else:
            raise ValueError(f"不支持的旧裁决种类：{kind}")
        converted.append(key)
    return {
        "schema_version": 2,
        "revision": document.get("revision", 0),
        "decisions": sorted(
            output.values(), key=lambda d: (d["id"], d["difficulty"], d["field_key"])
        ),
        "migration": {
            "converted_objects": converted,
            "converted_count": len(converted),
            "warnings": [],
        },
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
        return migrate_decisions(document, directory)
    return {**deepcopy(document), "schema_version": 2}


def _legacy_adapter_decisions(document):
    values = {}
    for choice in document.get("decisions", []):
        identity = choice.get("legacy_identity_decision")
        if identity:
            values[(identity["object_key"], identity["kind"])] = identity
    return {"revision": document.get("revision", 0), "decisions": list(values.values())}


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
                        if k
                        not in ("id", "charts", "api_version", "external_ids", "field_overrides")
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
            flat.pop("field_overrides", None)
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
                    adapted = {
                        k: deepcopy(v)
                        for k, v in option.items()
                        if k not in ("selectable", "raw_value")
                    }
                    adapted["value"] = deepcopy(value)
                    if is_jp:
                        adapted["date"] = option["value"]["date"]
                    options.append(adapted)
                values = {
                    fingerprint({"value": o["value"], "date": o.get("date")}) for o in options
                }
                if len(values) <= 1:
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
    try:
        from .build_catalog import override_index
        from .catalog_record_review import build_record_review
        from .catalog_versions import resolve_field_versions
    except ImportError:
        from build_catalog import override_index
        from catalog_record_review import build_record_review
        from catalog_versions import resolve_field_versions
    lxns = read(directory / "effective_lxns_songs.json", read(directory / "raw/lxns_songs.json"))
    arcade = read(directory / "raw/arcade_songs.json", {"songs": []})
    overrides, errors = override_index(directory / "remy_release_overrides.json")
    if errors:
        raise ValueError("Invalid Remy evidence index")
    versions = resolve_field_versions(
        lxns,
        arcade,
        list(overrides.values()),
        _legacy_adapter_decisions(document),
        read(directory / "remy_conflicts.json"),
    )
    review = build_record_review(
        lxns,
        read(directory / "raw/divingfish_music_data.json", []),
        arcade,
        version_review=versions,
        adjudications=_legacy_adapter_decisions(document),
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
    try:
        from .catalog_versions import empty_release
    except ImportError:
        from catalog_versions import empty_release
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


def write_sqlite(path, catalog):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(temporary) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE charts (
                id INTEGER NOT NULL, difficulty INTEGER NOT NULL, chart_key TEXT NOT NULL UNIQUE,
                legacy_chart_key TEXT NOT NULL UNIQUE, lxns_song_id INTEGER NOT NULL,
                type TEXT NOT NULL, difficulty_name TEXT NOT NULL, title TEXT NOT NULL, artist TEXT NOT NULL,
                bpm REAL, genre TEXT, level TEXT, constant REAL, note_designer TEXT, notes_json TEXT NOT NULL,
                cn_version INTEGER, api_version INTEGER, jp_release_version TEXT, jp_release_date TEXT,
                jp_release_provider TEXT, release_status TEXT, version_pending INTEGER NOT NULL,
                jacket_path TEXT NOT NULL, jacket_available INTEGER NOT NULL, jacket_source_json TEXT,
                aliases_json TEXT NOT NULL, source_ids_json TEXT NOT NULL, field_decisions_json TEXT NOT NULL,
                data_json TEXT NOT NULL, PRIMARY KEY(id,difficulty)
            );
            CREATE TABLE aliases (id INTEGER NOT NULL,difficulty INTEGER NOT NULL,alias TEXT NOT NULL,
                PRIMARY KEY(id,difficulty,alias),FOREIGN KEY(id,difficulty) REFERENCES charts(id,difficulty));
            CREATE TABLE external_ids (id INTEGER NOT NULL,difficulty INTEGER NOT NULL,namespace TEXT NOT NULL,
                external_id TEXT NOT NULL,type TEXT,source_title TEXT,
                FOREIGN KEY(id,difficulty) REFERENCES charts(id,difficulty));
            CREATE TABLE search_terms (id INTEGER NOT NULL,difficulty INTEGER NOT NULL,kind TEXT NOT NULL,term TEXT NOT NULL,
                FOREIGN KEY(id,difficulty) REFERENCES charts(id,difficulty));
            CREATE INDEX charts_constant ON charts(constant,id,difficulty);
        """)

        def encode(value):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

        for key, value in catalog.items():
            if key != "charts":
                conn.execute("INSERT INTO metadata VALUES (?,?)", (key, encode(value)))
        columns = [r[1] for r in conn.execute("PRAGMA table_info(charts)")]
        for chart in catalog["charts"]:
            values = []
            for column in columns:
                value = (
                    chart
                    if column == "data_json"
                    else chart[column[:-5]]
                    if column.endswith("_json")
                    else chart[column]
                )
                values.append(
                    encode(value)
                    if column.endswith("_json")
                    else int(value)
                    if isinstance(value, bool)
                    else value
                )
            conn.execute(
                "INSERT INTO charts VALUES (" + ",".join("?" for _ in values) + ")", values
            )
            pair = (chart["id"], chart["difficulty"])
            for alias in chart["aliases"]:
                conn.execute("INSERT INTO aliases VALUES (?,?,?)", (*pair, alias))
            for external in chart["external_ids"]:
                conn.execute(
                    "INSERT INTO external_ids VALUES (?,?,?,?,?,?)",
                    (
                        *pair,
                        external["namespace"],
                        external["id"],
                        external.get("type"),
                        external.get("source_title"),
                    ),
                )
            for kind, term in [
                ("title", chart["title"]),
                *[("alias", a) for a in chart["aliases"]],
            ]:
                conn.execute("INSERT INTO search_terms VALUES (?,?,?,?)", (*pair, kind, term))
        conn.commit()
        if (
            conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or conn.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("Schema 2 SQLite validation failed")
    conn.close()
    temporary.replace(path)


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
                "previous_catalog.json",
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


def validate(directory):
    """Recompute the admission boundary and every permanent exception from receipts."""
    directory = Path(directory)
    catalog = read(directory / "catalog.json")
    report = read(directory / "build_report.json")

    def require(condition, message):
        if not condition:
            raise ValueError("Catalog validation failed: " + message)

    require(
        catalog.get("schema_version") == 2 and "songs" not in catalog, "schema 2 flat chart catalog"
    )
    require(
        catalog.get("primary_source") == "lxns" and catalog.get("data_region") == "CN",
        "metadata authority",
    )
    require(
        catalog.get("source_policy") == source_policy() == report.get("source_policy"),
        "source policy",
    )
    for receipt in read(directory / "raw/sources.json", []):
        path = directory / "raw" / receipt["file"]
        require(
            path.is_file()
            and digest(path) == receipt["sha256"]
            and path.stat().st_size == receipt["bytes"],
            "raw source hash: " + receipt["file"],
        )
        try:
            from .catalog_pipeline import check_source
        except ImportError:
            from catalog_pipeline import check_source
        check_source(
            receipt["file"],
            read(path),
            allow_empty=receipt.get("fetch_status") in ("stale", "unavailable"),
        )
    for receipt in catalog.get("input_files", []):
        path = directory / receipt["path"]
        require(
            path.is_file()
            and digest(path) == receipt["sha256"]
            and path.stat().st_size == receipt["bytes"],
            "input hash: " + receipt["path"],
        )
    for receipt in report.get("outputs", []):
        path = directory / receipt["path"]
        require(
            path.is_file()
            and digest(path) == receipt["sha256"]
            and path.stat().st_size == receipt["bytes"],
            "output hash: " + receipt["path"],
        )
    if (directory / "effective_lxns_songs.json").exists():
        try:
            from .catalog_pipeline import merge_and_diff
        except ImportError:
            from catalog_pipeline import merge_and_diff
        effective, _ = merge_and_diff(
            read(directory / "raw/lxns_songs.json"),
            read(directory / "retained_lxns_records.json"),
            read(directory / "pipeline_report.json")["started_at"],
        )
        require(
            effective == read(directory / "effective_lxns_songs.json"),
            "effective raw/retained projection",
        )
    calculated = calculate(directory)
    require(
        catalog["charts"] == calculated["charts"],
        "admitted charts differ from source/decisions or pending chart leaked",
    )
    require(
        read(directory / "catalog_record_review.json") == calculated["review"],
        "pending review differs from source/decisions",
    )
    require(
        read(directory / "chart_key_map.json") == calculated["mappings"],
        "canonical chart identity mapping",
    )
    require(
        read(directory / "pending_charts.json")
        == {
            "schema_version": 2,
            "counts": calculated["review"]["counts"],
            "records": [r for r in calculated["review"]["records"] if r["status"] == "pending"],
        },
        "pending chart artifact",
    )
    require(
        catalog["decision_revision"]
        == report["decision_revision"]
        == calculated["document"].get("revision", 0),
        "decision revision",
    )
    for key, count in calculated["review"]["counts"].items():
        require(catalog["counts"][key] == count == report["counts"][key], "admission count " + key)
    require(catalog["counts"]["charts"] == len(catalog["charts"]), "formal chart count")
    require(
        catalog["versions_cn"] == calculated["lxns"]["versions"]
        and catalog["genres"] == calculated["lxns"]["genres"],
        "LXNS version/genre metadata",
    )
    by_key = {c["chart_key"]: c for c in catalog["charts"]}
    require(len(by_key) == len(catalog["charts"]), "unique chart keys")
    try:
        from .catalog_record_review import _notes
    except ImportError:
        from catalog_record_review import _notes
    for chart in catalog["charts"]:
        key = chart["chart_key"]
        require(key == f"{chart['id']}:{chart['difficulty']}", "canonical key " + key)
        require(chart["cn_version"] == chart["api_version"], "chart CN version " + key)
        require(
            "level_value" not in chart and "version" not in chart,
            "legacy raw business aliases " + key,
        )
        require(chart["admission_status"] == "admitted", "pending formal chart " + key)
        require(
            all(isinstance(chart[f], str) for f in ("title", "artist", "genre", "level")),
            "chart text fields " + key,
        )
        require(
            type(chart["bpm"]) in (int, float) and math.isfinite(chart["bpm"]), "chart BPM " + key
        )
        require(
            chart["constant"] is None
            or type(chart["constant"]) in (int, float)
            and math.isfinite(chart["constant"]),
            "chart constant " + key,
        )
        require(_notes(chart["notes"], chart["type"]) is not None, "chart notes " + key)
    # All source jackets remain validated even when their charts await admission.
    from PIL import Image

    jacket_report = read(directory / "jacket_manifest.json")
    jackets = {r["song_id"]: r for r in jacket_report["jackets"]}
    require(
        len(jackets) == len(jacket_report["jackets"]) == len(calculated["lxns"]["songs"]),
        "jacket coverage",
    )
    decoded = 0
    for song in calculated["lxns"]["songs"]:
        row = jackets[song["id"]]
        require(
            row["file"] == f"jackets/{song['id']}.png" and row["asset_id"] == song["id"] % 10000,
            "jacket resource mapping",
        )
        if row["status"] == "ok":
            path = directory / row["file"]
            require(
                path.is_file()
                and digest(path) == row["sha256"]
                and path.stat().st_size == row["bytes"],
                "jacket hash",
            )
            with Image.open(path) as image:
                require(
                    image.format == "PNG" and image.size == (row["width"], row["height"]),
                    "jacket image format",
                )
                image.load()
            decoded += 1
    for name in ("remy_release_overrides.json", "remy_conflicts.json"):
        evidence = read(directory / name, {})
        rows = (
            evidence.get("overrides", [])
            if name.endswith("overrides.json")
            else [c for r in evidence.get("conflicts", []) for c in r.get("candidates", [])]
        )
        if isinstance(rows, dict):
            rows = rows.values()
        for row in rows:
            if row.get("evidence_file") and row.get("evidence_sha256"):
                path = directory / row["evidence_file"]
                require(
                    path.is_file() and digest(path) == row["evidence_sha256"],
                    "Remy page evidence hash",
                )
    with closing(sqlite3.connect(f"file:{directory / 'catalog.sqlite'}?mode=ro", uri=True)) as conn:
        require(conn.execute("PRAGMA integrity_check").fetchone() == ("ok",), "SQLite integrity")
        require(not conn.execute("PRAGMA foreign_key_check").fetchall(), "SQLite FK")
        require(
            not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='songs'"
            ).fetchone(),
            "SQL songs entity is forbidden",
        )
        schema = conn.execute("PRAGMA table_info(charts)").fetchall()
        require(
            [(r[1], r[5]) for r in schema if r[5]] == [("id", 1), ("difficulty", 2)],
            "SQL chart composite primary key",
        )
        require(
            conn.execute("SELECT COUNT(*) FROM charts").fetchone()[0] == len(by_key),
            "SQL admitted count",
        )
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM charts"):
            require(row["chart_key"] in by_key, "SQL pending/unknown chart")
            chart = by_key[row["chart_key"]]
            for field in row.keys():
                expected = (
                    chart
                    if field == "data_json"
                    else chart[field[:-5]]
                    if field.endswith("_json")
                    else chart[field]
                )
                value = json.loads(row[field]) if field.endswith("_json") else row[field]
                require(value == expected, "SQL column mismatch: " + field)
        metadata = {
            r[0]: json.loads(r[1]) for r in conn.execute("SELECT key,value_json FROM metadata")
        }
        require(metadata == {k: v for k, v in catalog.items() if k != "charts"}, "SQL metadata")
    validation = {
        "schema_version": 2,
        "status": "ok",
        "charts_checked": len(by_key),
        "total_charts": calculated["review"]["counts"]["total"],
        "pending_charts": calculated["review"]["counts"]["pending"],
        "images_decoded_and_hash_checked": decoded,
        "sqlite_integrity": "ok",
        "sqlite_foreign_key_errors": 0,
    }
    write(directory / "validation_report.json", validation)
    return validation
