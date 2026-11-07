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


def exact_identity(candidates, title, artist):
    """Shared identity filtering for JP releases and record field comparisons."""
    return [
        c
        for c in candidates
        if not c.get("identity_conflict")
        and normalize(c["title"]) == normalize(title)
        and normalize(c["artist"]) == normalize(artist)
    ]


def _adopt(candidate, scope, fp):
    return {
        "jp_release_version": candidate["version"],
        "jp_release_date": candidate.get("date"),
        "jp_release_source": candidate.get("source_url"),
        "jp_release_provider": candidate["source"],
        "jp_release_evidence": json.dumps(
            candidate.get("evidence", {}), ensure_ascii=False, sort_keys=True
        ),
        "release_status": "verified" if candidate["source"] == "remywiki" else "resolved",
        "release_source_scope": scope,
        "jp_release_provenance": {
            "object_fingerprint": fp,
            "candidate": deepcopy(candidate),
            "decision": None,
        },
    }


def resolve_versions(lxns, arcade, remy_records, remy_conflicts=None, decision_revision=0):
    """Return all version objects and final release fields without mutating inputs."""
    candidates = arcade_candidates(arcade)
    by_title_type = defaultdict(list)
    for candidate in candidates:
        by_title_type[(normalize(candidate["title"]), candidate["type"])].append(candidate)
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
                reasons = []
                exact = exact_identity(potential, song["title"], song["artist"])
                if len(exact) != 1:
                    reasons.append(
                        "identity_ambiguous"
                        if len(exact) > 1
                        else "artist_mismatch"
                        if potential
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
                release = _adopt(proposed, scope, fp) if proposed else empty_release()
                if key in conflict_keys:
                    reasons.append("remy_evidence_conflict")
                    release = empty_release()
                    release["release_status"] = "conflict"
                if len(exact) > 1 and not release["jp_release_version"]:
                    release["release_status"] = "conflict"
                status = (
                    "conflict"
                    if release["release_status"] == "conflict"
                    or len(exact) > 1
                    or key in conflict_keys
                    else "resolved"
                    if release["jp_release_version"]
                    else "pending"
                )
                item = {
                    "object_key": key,
                    "scope": scope,
                    "song_id": song["id"],
                    "title": song["title"],
                    "artist": song["artist"],
                    "type": kind,
                    "difficulty_index": difficulty,
                    "object_fingerprint": fp,
                    "current": {
                        "version": release["jp_release_version"],
                        "date": release["jp_release_date"],
                        "provider": release["jp_release_provider"],
                        "status": release["release_status"],
                    },
                    "candidates": potential,
                    "reasons": sorted(set(reasons)),
                    "status": status,
                }
                objects[key] = release
                items.append(item)
                if status in ("conflict", "pending"):
                    issues.append(
                        {
                            "kind": "jp_version_" + status,
                            "object_key": key,
                            "reasons": item["reasons"],
                        }
                    )
    return {
        "schema_version": 1,
        "decision_revision": decision_revision,
        "objects": objects,
        "items": items,
        "issues": issues,
    }


def resolve_field_versions(lxns, arcade, remy_records, remy_conflicts=None, decision_revision=0):
    """Resolve source values for chart fields; linkage status is never a value conflict.

    Historical published values are not another source. Permanent manual choices
    are applied by the chart compiler after this source projection.
    """
    result = resolve_versions(lxns, arcade, remy_records, remy_conflicts, decision_revision)
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
