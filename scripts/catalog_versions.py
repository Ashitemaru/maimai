"""Deterministic JP release evidence and user adjudication; no network or LLM."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict
from copy import deepcopy
from datetime import date
from urllib.parse import urlsplit

ARCADE_URL = "https://dp4p6x0xfi5o9.cloudfront.net/maimai/data.json"
ORDINARY = {"basic": 0, "advanced": 1, "expert": 2, "master": 3}
KNOWN_VERSIONS = (
    "maimai",
    "maimai PLUS",
    "GreeN",
    "GreeN PLUS",
    "ORANGE",
    "ORANGE PLUS",
    "PiNK",
    "PiNK PLUS",
    "MURASAKi",
    "MURASAKi PLUS",
    "MiLK",
    "MiLK PLUS",
    "FiNALE",
    "DX",
    "DX PLUS",
    "Splash",
    "Splash PLUS",
    "UNiVERSE",
    "UNiVERSE PLUS",
    "FESTiVAL",
    "FESTiVAL PLUS",
    "BUDDiES",
    "BUDDiES PLUS",
    "PRiSM",
    "PRiSM PLUS",
    "CiRCLE",
    "CiRCLE PLUS",
    "MAGiCAL",
)
RELEASE_FIELDS = (
    "jp_release_version",
    "jp_release_date",
    "jp_release_source",
    "jp_release_provider",
    "jp_release_evidence",
    "release_status",
    "release_source_scope",
    "jp_release_provenance",
)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize(value):
    return "".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def version_name(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.strip().split())
    return {
        "maimaiでらっくす": "DX",
        "maimai でらっくす": "DX",
        "maimaiでらっくす PLUS": "DX PLUS",
        "maimai でらっくす PLUS": "DX PLUS",
    }.get(text, text)


def object_fingerprint(song, kind):
    return fingerprint(
        {"song_id": song["id"], "title": song["title"], "artist": song["artist"], "type": kind}
    )


def empty_release():
    return {
        "jp_release_version": None,
        "jp_release_date": None,
        "jp_release_source": None,
        "jp_release_provider": None,
        "jp_release_evidence": None,
        "release_status": "unresolved",
        "release_source_scope": None,
        "jp_release_provenance": None,
    }


def _candidate(
    source, external_id, kind, title, artist, version, release_date, evidence, source_url
):
    version = version_name(version)
    body = {
        "source": source,
        "external_id": str(external_id),
        "type": kind,
        "title": title,
        "artist": artist,
        "version": version,
        "date": release_date,
        "evidence": evidence,
        "source_url": source_url,
    }
    body["candidate_id"] = (
        source
        + ":"
        + fingerprint(
            {
                "source": source,
                "external_id": str(external_id),
                "type": kind,
                "version": version,
                "date": release_date,
            }
        )[:24]
    )
    body["evidence_fingerprint"] = fingerprint(
        {
            k: body[k]
            for k in (
                "source",
                "external_id",
                "type",
                "title",
                "artist",
                "version",
                "date",
                "evidence",
                "source_url",
            )
        }
    )
    return body


def arcade_candidates(document):
    """Only sheets.version is JP format evidence; neither song.version nor region overrides are consulted."""
    rows = document.get("songs", []) if isinstance(document, dict) else []
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(k), str) for k in ("songId", "title", "artist")
        ):
            continue
        for source_type, kind in (("std", "standard"), ("dx", "dx")):
            sheets = [
                sheet
                for sheet in row.get("sheets", [])
                if sheet.get("type") == source_type and sheet.get("difficulty") in ORDINARY
            ]
            if not sheets:
                continue
            sheets.sort(
                key=lambda sheet: (ORDINARY[sheet["difficulty"]], str(sheet.get("version")))
            )
            versions = {version_name(sheet.get("version")) for sheet in sheets}
            present = {sheet["difficulty"] for sheet in sheets}
            # Store separate versions as conflicting candidates, never choose one by frequency.
            for version in sorted(versions, key=lambda value: value or ""):
                evidence = {
                    "field": "sheets[].version",
                    "source_sheet_type": source_type,
                    "sheets": [
                        {"difficulty": s["difficulty"], "version": s.get("version")} for s in sheets
                    ],
                    "version_consistent": len(versions) == 1,
                    "ordinary_difficulties": sorted(present),
                }
                candidates.append(
                    _candidate(
                        "arcade_songs",
                        row["songId"],
                        kind,
                        row["title"],
                        row["artist"],
                        version,
                        None,
                        evidence,
                        ARCADE_URL,
                    )
                )
    return candidates


def remy_candidate(record, song, kind):
    source = record.get("source_url", record.get("jp_release_source"))
    version = record.get("jp_release_version")
    release_date = record.get("jp_release_date")
    try:
        parsed = urlsplit(source or "")
        if (
            parsed.hostname != "silentblue.remywiki.com"
            or parsed.scheme not in ("http", "https")
            or parsed.username
            or parsed.password
        ):
            return None
        date.fromisoformat(release_date)
    except (TypeError, ValueError):
        return None
    if (
        not version
        or not record.get("evidence")
        or (
            record.get("verified") is not True and record.get("match_status") != "identity_conflict"
        )
        or record.get("source_provider", "remywiki") != "remywiki"
    ):
        return None
    key = (
        record.get("chart_key")
        or f"{record.get('song_id')}:{record.get('type', record.get('chart_type'))}:4"
    )
    if key != f"{song['id']}:{kind}:4":
        return None
    evidence = {
        k: record[k]
        for k in ("evidence", "evidence_row", "source_revision", "evidence_sha256", "match_method")
        if k in record
    }
    result = _candidate(
        "remywiki",
        key,
        kind,
        record.get("title", song["title"]),
        record.get("artist", song["artist"]),
        version,
        release_date,
        evidence,
        source,
    )
    result["identity_conflict"] = record.get("match_status") == "identity_conflict"
    return result


def _decision_item(review, key):
    return next((row for row in review.get("items", []) if row["object_key"] == key), None)


def validate_decision(review, decision):
    """Validate a requested active decision and bind it to current identity/evidence."""
    if not isinstance(decision, dict):
        raise ValueError("裁决格式无效")
    item = _decision_item(review, decision.get("object_key"))
    if not item:
        raise ValueError("待裁决对象不存在")
    result = deepcopy(decision)
    if result.get("object_fingerprint") not in (None, item["object_fingerprint"]):
        raise ValueError("曲目身份已变化，请重新打开后裁决")
    result["object_fingerprint"] = item["object_fingerprint"]
    kind, action = result.get("kind"), result.get("action")
    if kind not in ("identity", "version") or action not in ("select", "reject", "set", "keep"):
        raise ValueError("裁决类型无效")
    if kind == "identity" and action not in ("select", "reject"):
        raise ValueError("身份裁决只能选择或拒绝候选")
    if kind == "version":
        context_fp = fingerprint(sorted({c["evidence_fingerprint"] for c in item["candidates"]}))
        if result.get("evidence_context_fingerprint") not in (None, context_fp):
            raise ValueError("来源证据已变化，请重新确认版本")
        result["evidence_context_fingerprint"] = context_fp
    candidate = next(
        (c for c in item["candidates"] if c["candidate_id"] == result.get("candidate_id")), None
    )
    if action == "select" or result.get("candidate_id"):
        if not candidate:
            raise ValueError("候选证据已变化或不存在")
        if action == "select" and kind == "version" and not candidate["version"]:
            raise ValueError("该候选没有可采用的日服版本")
        expected = candidate["evidence_fingerprint"]
    elif action == "keep":
        if not item["current"].get("version"):
            raise ValueError("当前没有可保留的版本")
        expected = fingerprint(
            {"version": item["current"]["version"], "date": item["current"].get("date")}
        )
        result["version"] = item["current"]["version"]
        result["date"] = item["current"].get("date")
    elif action == "reject":
        expected = fingerprint(sorted(c["evidence_fingerprint"] for c in item["candidates"]))
    else:
        expected = None
    if expected is not None:
        if result.get("evidence_fingerprint") not in (None, expected):
            raise ValueError("候选证据已变化，请重新裁决")
        result["evidence_fingerprint"] = expected
    if action == "set":
        value = version_name(result.get("version"))
        if not value or len(value) > 100 or any(ord(ch) < 32 for ch in value):
            raise ValueError("请输入明确的日服版本")
        result["version"] = value
    if action in ("set", "keep") and result.get("date"):
        try:
            date.fromisoformat(result["date"])
        except (ValueError, TypeError):
            raise ValueError("日期格式应为 YYYY-MM-DD") from None
    return result


def validate_bound_decision(item, decision):
    """An existing decision must already bind the exact identity and evidence."""
    result = validate_decision({"items": [item]}, decision)
    for key in ("object_fingerprint", "evidence_fingerprint", "evidence_context_fingerprint"):
        if key in result and decision.get(key) != result[key]:
            raise ValueError("unbound decision")
    return result


def identity_candidates(potential, title, artist, identity=None):
    """Shared identity filtering for JP releases and record field comparisons."""

    def exact(rows):
        return [
            c
            for c in rows
            if not c.get("identity_conflict")
            and normalize(c["title"]) == normalize(title)
            and normalize(c["artist"]) == normalize(artist)
        ]

    natural = exact(potential)
    eligible = potential
    if identity:
        if identity["action"] == "select":
            eligible = [c for c in potential if c["candidate_id"] == identity["candidate_id"]]
        else:
            eligible = (
                [c for c in potential if c["candidate_id"] != identity.get("candidate_id")]
                if identity.get("candidate_id")
                else []
            )
    selected = eligible if identity and identity["action"] == "select" else exact(eligible)
    return natural, eligible, selected


def _previous(previous_catalog):
    old = {}
    for song in (previous_catalog or {}).get("songs", []):
        for chart in song.get("charts", []):
            kind, index = chart["type"], chart["difficulty_index"]
            if kind not in ("standard", "dx") or index not in range(5):
                continue
            key = f"{song['id']}:{kind}:ordinary" if index < 4 else chart["chart_key"]
            if chart.get("jp_release_version") and chart.get("jp_release_provider") in (
                "arcade_songs",
                "remywiki",
                "manual",
            ):
                old.setdefault(key, (song, chart))
    return old


def _adopt(candidate, scope, fp, *, manual=False, decision=None, identity_decision=None):
    provenance = {
        "object_fingerprint": fp,
        "candidate": deepcopy(candidate),
        "decision": deepcopy(decision),
    }
    if identity_decision:
        provenance["identity_decision"] = deepcopy(identity_decision)
    return {
        "jp_release_version": candidate["version"],
        "jp_release_date": candidate.get("date"),
        "jp_release_source": candidate.get("source_url"),
        "jp_release_provider": "manual" if manual else candidate["source"],
        "jp_release_evidence": json.dumps(
            candidate.get("evidence", {}), ensure_ascii=False, sort_keys=True
        ),
        "release_status": "manual"
        if manual
        else "verified"
        if candidate["source"] == "remywiki"
        else "resolved",
        "release_source_scope": scope,
        "jp_release_provenance": provenance,
    }


def _same_decision_binding(left, right):
    fields = (
        "object_key",
        "kind",
        "action",
        "candidate_id",
        "object_fingerprint",
        "evidence_fingerprint",
    )
    return all(left.get(field) == right.get(field) for field in fields)


def _rejects_adopted_candidate(decision, candidate, potential):
    if not decision or decision["action"] != "reject" or not candidate:
        return False
    rejected = [
        row
        for row in potential
        if not decision.get("candidate_id") or row["candidate_id"] == decision["candidate_id"]
    ]
    return any(
        all(
            row.get(field) == candidate.get(field)
            for field in ("candidate_id", "evidence_fingerprint")
        )
        for row in rejected
    )


def resolve_versions(
    lxns, arcade, remy_records, previous_catalog=None, adjudications=None, remy_conflicts=None
):
    """Return all version objects and final release fields without mutating inputs."""
    candidates = arcade_candidates(arcade)
    by_title_type = defaultdict(list)
    for candidate in candidates:
        by_title_type[(normalize(candidate["title"]), candidate["type"])].append(candidate)
    old = _previous(previous_catalog)
    decisions = defaultdict(list)
    for row in (adjudications or {}).get("decisions", []):
        decisions[row.get("object_key")].append(deepcopy(row))
    records = list(remy_records or [])
    conflict_keys = set()
    for conflict in (remy_conflicts or {}).get("conflicts", []):
        conflict_keys.add(conflict["chart_key"])
        records.extend(conflict.get("candidates", []))
    objects, items, issues = {}, [], []
    for song in lxns.get("songs", []):
        for kind in ("standard", "dx"):
            charts = song["difficulties"].get(kind, [])
            scopes = []
            if any(c["difficulty"] in range(4) for c in charts):
                scopes.append(("ordinary_format", f"{song['id']}:{kind}:ordinary", None))
            if any(c["difficulty"] == 4 for c in charts):
                scopes.append(("chart", f"{song['id']}:{kind}:4", 4))
            for scope, key, difficulty in scopes:
                fp = object_fingerprint(song, kind)
                potential = (
                    deepcopy(by_title_type[(normalize(song["title"]), kind)])
                    if scope == "ordinary_format"
                    else [c for r in records if (c := remy_candidate(r, song, kind))]
                )
                # Exact duplicate evidence is harmless; differing evidence with the same identity needs a review.
                unique = {}
                for candidate in potential:
                    unique.setdefault(candidate["evidence_fingerprint"], candidate)
                potential = list(unique.values())
                counts = defaultdict(int)
                for c in potential:
                    counts[c["candidate_id"]] += 1
                for c in potential:
                    if counts[c["candidate_id"]] > 1:
                        c["candidate_id"] += ":" + c["evidence_fingerprint"][:12]
                prior = old.get(key)
                old_release = empty_release()
                if prior:
                    old_release = {field: deepcopy(prior[1].get(field)) for field in RELEASE_FIELDS}
                    old_release["release_source_scope"] = scope
                current = {
                    "version": old_release["jp_release_version"],
                    "date": old_release["jp_release_date"],
                    "provider": old_release["jp_release_provider"],
                    "status": old_release["release_status"],
                }
                item = {
                    "object_key": key,
                    "scope": scope,
                    "song_id": song["id"],
                    "title": song["title"],
                    "artist": song["artist"],
                    "type": kind,
                    "difficulty_index": difficulty,
                    "object_fingerprint": fp,
                    "current": current,
                    "candidates": potential,
                    "reasons": [],
                    "status": "pending",
                    "decisions": decisions[key],
                }
                reasons = item["reasons"]
                active = {}
                stale = False
                for decision in decisions[key]:
                    try:
                        normalized_decision = validate_bound_decision(item, decision)
                        if decision["kind"] in active:
                            raise ValueError("duplicate active decision")
                        active[decision["kind"]] = normalized_decision
                    except ValueError:
                        stale = True
                        reasons.append("decision_stale")
                identity = active.get("identity")
                version_decision = active.get("version")
                natural_exact, eligible, exact = identity_candidates(
                    potential, song["title"], song["artist"], identity
                )
                if identity and identity["action"] == "reject":
                    reasons.append("identity_rejected")
                if len(exact) != 1:
                    reasons.append(
                        "identity_ambiguous"
                        if len(exact) > 1
                        else "artist_mismatch"
                        if eligible
                        else "no_source_evidence"
                    )
                proposed = exact[0] if len(exact) == 1 and exact[0]["version"] else None
                if len(exact) == 1 and not exact[0]["version"]:
                    reasons.append(
                        "missing_sheet_version"
                        if scope == "ordinary_format"
                        else "missing_remy_version"
                    )
                if proposed and proposed["version"] not in KNOWN_VERSIONS:
                    proposed = None
                    reasons.append("unknown_jp_version")
                if (
                    scope == "ordinary_format"
                    and proposed
                    and not proposed["evidence"]["version_consistent"]
                ):
                    proposed = None
                    reasons.append("source_version_conflict")
                release = empty_release()
                prior_automatic = prior and old_release["jp_release_provider"] in (
                    "arcade_songs",
                    "remywiki",
                )
                prior_provenance = old_release.get("jp_release_provenance") or {}
                identity_dependency = prior_provenance.get("identity_decision")
                if identity_dependency and not any(
                    _same_decision_binding(identity_dependency, row) for row in decisions[key]
                ):
                    prior_automatic = False
                    reasons.append("adopted_identity_decision_removed")
                if any(
                    _rejects_adopted_candidate(
                        decision, prior_provenance.get("candidate"), potential
                    )
                    for decision in (identity, version_decision)
                ):
                    prior_automatic = False
                    reasons.append("adopted_candidate_rejected")
                prior_manual_retained = (
                    prior
                    and old_release["jp_release_provider"] == "manual"
                    and any(row.get("kind") == "version" for row in decisions[key])
                )
                # Display metadata exceptions do not change the raw source identity
                # against which the previously adopted version was adjudicated.
                prior_identity = prior[0].get("source_identity", prior[0]) if prior else None
                prior_changed = (
                    prior
                    and prior_provenance.get(
                        "object_fingerprint", object_fingerprint(prior_identity, kind)
                    )
                    != fp
                )
                if prior_changed:
                    reasons.append("object_identity_changed")
                if proposed and not stale and not prior_changed:
                    dependency = (
                        identity
                        if identity and (len(natural_exact) != 1 or natural_exact[0] != proposed)
                        else None
                    )
                    release = _adopt(proposed, scope, fp, identity_decision=dependency)
                if prior_automatic:
                    changed = not proposed or (proposed["version"], proposed["date"]) != (
                        current["version"],
                        current["date"],
                    )
                    if changed or stale or prior_changed:
                        release = old_release
                        release["release_status"] = "conflict"
                        reasons.append(
                            "adopted_version_changed" if proposed else "adopted_evidence_missing"
                        )
                elif prior_manual_retained and (stale or prior_changed):
                    release = old_release
                    release["release_status"] = "conflict"
                if key in conflict_keys:
                    reasons.append("remy_evidence_conflict")
                    if not version_decision:
                        release = (
                            old_release
                            if prior_automatic or prior_manual_retained
                            else empty_release()
                        )
                        release["release_status"] = "conflict"
                if version_decision:
                    action = version_decision["action"]
                    if action == "select":
                        selected = next(
                            c
                            for c in potential
                            if c["candidate_id"] == version_decision["candidate_id"]
                        )
                        release = _adopt(
                            selected, scope, fp, manual=True, decision=version_decision
                        )
                    elif action in ("set", "keep"):
                        selected = {
                            "version": version_decision["version"],
                            "date": version_decision.get("date"),
                            "source": "manual",
                            "source_url": None,
                            "evidence": {
                                "note": version_decision.get("note", ""),
                                "action": action,
                            },
                        }
                        release = _adopt(
                            selected, scope, fp, manual=True, decision=version_decision
                        )
                    elif action == "reject":
                        rejected = version_decision.get("candidate_id")
                        if not rejected or proposed and proposed["candidate_id"] == rejected:
                            release = old_release if prior_automatic else empty_release()
                            release["release_status"] = (
                                "conflict" if release["jp_release_version"] else "unresolved"
                            )
                        reasons.append("version_rejected")
                if len(exact) > 1 and not version_decision and not release["jp_release_version"]:
                    release["release_status"] = "conflict"
                if stale and release["release_status"] not in ("conflict", "manual"):
                    release = empty_release()
                item["current"] = {
                    "version": release["jp_release_version"],
                    "date": release["jp_release_date"],
                    "provider": release["jp_release_provider"],
                    "status": release["release_status"],
                }
                item["status"] = (
                    "manual"
                    if release["release_status"] == "manual"
                    else "conflict"
                    if release["release_status"] == "conflict"
                    or len(exact) > 1
                    or key in conflict_keys
                    else "resolved"
                    if release["jp_release_version"]
                    else "pending"
                )
                item["reasons"] = sorted(set(reasons))
                objects[key] = release
                items.append(item)
                if item["status"] in ("conflict", "pending"):
                    issues.append(
                        {
                            "kind": "jp_version_" + item["status"],
                            "object_key": key,
                            "reasons": item["reasons"],
                        }
                    )
    return {
        "schema_version": 1,
        "decision_revision": (adjudications or {}).get("revision", 0),
        "objects": objects,
        "items": items,
        "issues": issues,
    }


def resolve_field_versions(lxns, arcade, remy_records, adjudications=None, remy_conflicts=None):
    """Resolve source values for chart fields; linkage status is never a value conflict.

    Historical published values are not another source. Permanent manual choices
    are applied by the chart compiler after this source projection.
    """
    result = resolve_versions(
        lxns, arcade, remy_records, adjudications=adjudications, remy_conflicts=remy_conflicts
    )
    songs = {song["id"]: song for song in lxns["songs"]}
    local_title_types = defaultdict(set)
    for song in lxns["songs"]:
        for kind in ("standard", "dx"):
            if song["difficulties"].get(kind):
                local_title_types[(normalize(song["title"]), kind)].add(song["id"])
    issues = []
    for item in result["items"]:
        song = songs[item["song_id"]]
        candidates = item["candidates"]
        linked = []
        if item["scope"] == "ordinary_format":
            exact = [c for c in candidates if normalize(c["artist"]) == normalize(song["artist"])]
            if exact:
                linked = exact
            else:
                source_records = {(c["external_id"], normalize(c["artist"])) for c in candidates}
                local_ids = local_title_types[(normalize(song["title"]), item["type"])]
                if len(source_records) == 1 and len(local_ids) == 1:
                    # Author wording helps disambiguate identities, but is not
                    # imported into catalog metadata or compared as a field.
                    linked = candidates
        else:
            linked = [
                c
                for c in candidates
                if not c.get("identity_conflict")
                and normalize(c["title"]) == normalize(song["title"])
            ]

        item["unlinked_candidates"] = [deepcopy(c) for c in candidates if c not in linked]
        item["candidates"] = linked
        values = {}
        for candidate in linked:
            if candidate["version"]:
                values.setdefault((candidate["version"], candidate.get("date")), []).append(
                    candidate
                )
        release = empty_release()
        if len(values) == 1:
            evidence = next(iter(values.values()))
            selected = min(
                evidence, key=lambda c: (c["source"], c["external_id"], c["evidence_fingerprint"])
            )
            release = _adopt(selected, item["scope"], item["object_fingerprint"])
            item["status"], item["reasons"] = "resolved", []
        elif len(values) > 1:
            release["release_status"] = "conflict"
            item["status"], item["reasons"] = "conflict", ["source_value_conflict"]
        else:
            item["status"] = "pending"
            item["reasons"] = [
                "source_link_unresolved" if candidates and not linked else "no_source_evidence"
            ]
        item["current"] = {
            "version": release["jp_release_version"],
            "date": release["jp_release_date"],
            "provider": release["jp_release_provider"],
            "status": release["release_status"],
        }
        result["objects"][item["object_key"]] = release
        if item["status"] != "resolved":
            issues.append(
                {
                    "kind": "jp_version_" + item["status"],
                    "object_key": item["object_key"],
                    "reasons": item["reasons"],
                }
            )
    result["issues"] = issues
    return result
