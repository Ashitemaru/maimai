import copy
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts.build_catalog import build_v1 as build
from scripts.build_catalog import jp_version_name, write_sqlite
from scripts.catalog_pipeline import (
    SOURCES,
    atomic_json,
    check_source,
    merge_and_diff,
    prepare_update,
)
from scripts.catalog_policy import source_policy
from scripts.catalog_record_review import (
    apply_field_decisions,
    build_record_review,
    load_record_review,
    validate_field_decision,
)
from scripts.catalog_versions import resolve_versions, validate_decision
from scripts.validate_catalog import validate_v1 as validate


def song(sid=1, constant=13.0, remaster=False):
    chart = {
        "type": "standard",
        "difficulty": 4 if remaster else 3,
        "level": "13",
        "level_value": constant,
        "note_designer": "Designer",
        "version": 10000,
        "notes": {"total": 1, "tap": 1, "hold": 0, "slide": 0, "touch": 0, "break": 0},
    }
    return {
        "id": sid,
        "title": f"Song {sid}",
        "artist": "Artist",
        "genre": "maimai",
        "bpm": 120,
        "version": 10000,
        "difficulties": {"standard": [chart], "dx": []},
    }


def document(songs):
    return {"songs": songs, "versions": [{"version": 10000, "title": "maimai"}], "genres": []}


def png():
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(stream, "PNG")
    return stream.getvalue()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.previous = Path(self.temp.name) / "previous"
        self.stage = Path(self.temp.name) / "stage"
        self.previous.mkdir()
        self.old = document([song(1), song(2, remaster=True)])
        self.sources = {
            "lxns_songs.json": self.old,
            "lxns_aliases.json": {"aliases": []},
            "divingfish_music_data.json": [],
            "arcade_songs.json": {"songs": []},
        }
        receipts = []
        for name, value in self.sources.items():
            path = self.previous / "raw" / name
            atomic_json(path, value)
            content = path.read_bytes()
            receipts.append(
                {
                    "file": name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                    "fetched_at": "2020-01-01T00:00:00Z",
                }
            )
        atomic_json(self.previous / "raw/sources.json", receipts)
        atomic_json(self.previous / "remy_release_overrides.json", {"overrides": []})
        shutil.copytree(self.previous, self.stage, copy_function=os.link)
        self.responses = copy.deepcopy(self.sources)
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        for name, source_url, *_ in SOURCES:
            if url == source_url:
                value = self.responses[name]
                if isinstance(value, Exception):
                    raise value
                return json.dumps(value).encode(), {}
        return png(), {}

    def prepare(self):
        with (
            patch("scripts.catalog_pipeline.fetch", side_effect=self.fetch),
            patch("scripts.catalog_pipeline.time.sleep"),
        ):
            return prepare_update(self.stage, self.previous)

    def test_added_changed_missing_preserved_raw_and_hardlinks_safe(self):
        current_song = song(1, 13.3)
        current_song["difficulties"][
            "standard"
        ] = []  # vanished old chart remains pending, rather than removed
        self.responses["lxns_songs.json"] = document([current_song, song(3, 14)])
        before = {
            p.relative_to(self.previous): p.read_bytes()
            for p in self.previous.rglob("*")
            if p.is_file()
        }
        report = self.prepare()
        build(self.stage)
        validation = validate(self.stage)
        self.assertEqual(validation["songs_checked"], 3)
        self.assertEqual(validation["charts_checked"], 3)
        raw = json.loads((self.stage / "raw/lxns_songs.json").read_text())
        self.assertEqual(raw, self.responses["lxns_songs.json"])
        effective = json.loads((self.stage / "effective_lxns_songs.json").read_text())
        by_id = {s["id"]: s for s in effective["songs"]}
        self.assertEqual(by_id[2]["upstream_status"], "missing")
        self.assertEqual(by_id[1]["difficulties"]["standard"][0]["upstream_status"], "missing")
        self.assertEqual(report["diff"]["counts"]["added_songs"], 1)
        self.assertEqual(report["diff"]["counts"]["missing_charts"], 2)
        for relative, content in before.items():
            self.assertEqual((self.previous / relative).read_bytes(), content, relative)

    def test_optional_sources_fail_without_blocking_authority(self):
        self.responses["divingfish_music_data.json"] = TimeoutError("slow")
        self.responses["arcade_songs.json"] = ValueError("bad response")
        report = self.prepare()
        self.assertEqual(len(report["warnings"]), 2)
        self.assertTrue(all(r["fetch_status"] == "unavailable" for r in report["sources"][2:]))
        build(self.stage)
        self.assertEqual(validate(self.stage)["status"], "ok")

    def test_optional_valid_cache_reused_with_original_fetch_time(self):
        previous_fish = [
            {
                "id": "1",
                "title": "Song 1",
                "type": "SD",
                "basic_info": {"artist": "Artist", "from": "maimai"},
                "ds": [1, 2, 3, 14.9],
            }
        ]
        atomic_json(self.previous / "raw/divingfish_music_data.json", previous_fish)
        self.responses["divingfish_music_data.json"] = TimeoutError("slow")
        result = self.prepare()
        self.assertEqual(result["sources"][2]["fetch_status"], "stale")
        self.assertEqual(result["sources"][2]["fetched_at"], "2020-01-01T00:00:00Z")
        report = build(self.stage)
        self.assertTrue(any(i["kind"] == "external_constant_difference" for i in report["issues"]))
        self.assertEqual(
            json.loads((self.stage / "catalog.json").read_text())["songs"][0]["charts"][0][
                "constant"
            ],
            13.0,
        )
        validate(self.stage)

    def test_authority_failure_leaves_inherited_files_unchanged(self):
        original = (self.stage / "raw/lxns_songs.json").read_bytes()
        self.responses["lxns_aliases.json"] = RuntimeError("denied")
        with self.assertRaisesRegex(RuntimeError, "落雪"):
            self.prepare()
        self.assertEqual((self.stage / "raw/lxns_songs.json").read_bytes(), original)

    def test_missing_jacket_is_reported_and_validates(self):
        source_fetch = self.fetch

        def failing_image(url):
            if "jacket/" in url:
                raise OSError("404")
            return source_fetch(url)

        with (
            patch("scripts.catalog_pipeline.fetch", side_effect=failing_image),
            patch("scripts.catalog_pipeline.time.sleep"),
        ):
            result = prepare_update(self.stage, self.previous)
        self.assertEqual(result["counts"]["jackets_missing"], 2)
        build(self.stage)
        self.assertEqual(validate(self.stage)["jackets_missing"], [1, 2])

    def test_supplement_is_cross_validation_only_and_remy_can_be_retired(self):
        record = {
            "chart_key": "2:standard:4",
            "verified": True,
            "jp_release_version": "PiNK",
            "jp_release_date": "2016-01-01",
            "source_url": "https://gamerch.com/maimai/123",
            "evidence": "test",
        }
        atomic_json(self.stage / "supplemental_release_overrides.json", {"overrides": [record]})
        retired = {
            **record,
            "chart_key": "999:standard:4",
            "source_url": "https://silentblue.remywiki.com/maimai:PiNK",
        }
        atomic_json(self.stage / "remy_release_overrides.json", {"overrides": [retired]})
        self.prepare()
        build(self.stage)
        result = validate(self.stage)
        self.assertEqual(result["remaster_unresolved"], 1)
        self.assertEqual(result["remaster_verified"], 0)
        self.assertEqual(result["retired_remy_records"], ["999:standard:4"])

    def test_unverified_remy_is_pending_not_promoted(self):
        atomic_json(
            self.stage / "remy_release_overrides.json",
            {"overrides": [{"chart_key": "2:standard:4", "verified": False}]},
        )
        self.prepare()
        build(self.stage)
        self.assertEqual(validate(self.stage)["remaster_unresolved"], 1)

    def test_validator_detects_tampering(self):
        self.prepare()
        build(self.stage)
        atomic_json(
            self.stage / "raw/lxns_aliases.json",
            {"aliases": [{"song_id": 1, "aliases": ["tampered"]}]},
        )
        with self.assertRaisesRegex(ValueError, "raw source hash"):
            validate(self.stage)

    def test_validator_rejects_sql_columns_when_json_and_hashes_are_valid(self):
        self.prepare()
        build(self.stage)
        original = (self.stage / "catalog.sqlite").read_bytes()
        for statement, column in (
            ("UPDATE charts SET constant=99 WHERE chart_key='1:standard:3'", "charts.constant"),
            ("UPDATE songs SET jacket_available=0 WHERE id=1", "songs.jacket_available"),
            (
                "UPDATE charts SET notes_json='{}' WHERE chart_key='1:standard:3'",
                "charts.notes_json",
            ),
        ):
            with self.subTest(column=column):
                (self.stage / "catalog.sqlite").write_bytes(original)
                with closing(sqlite3.connect(self.stage / "catalog.sqlite")) as conn:
                    conn.execute(statement)
                    conn.commit()
                report = json.loads((self.stage / "build_report.json").read_text())
                for output in report["outputs"]:
                    if output["path"] == "catalog.sqlite":
                        output["sha256"] = hashlib.sha256(
                            (self.stage / "catalog.sqlite").read_bytes()
                        ).hexdigest()
                atomic_json(self.stage / "build_report.json", report)
                with self.assertRaisesRegex(ValueError, column):
                    validate(self.stage)

    def test_validator_checks_remy_conflict_candidate_page_hash(self):
        self.prepare()
        evidence = self.stage / "remy-test.html"
        evidence.write_text("original evidence")
        row = {
            "chart_key": "2:standard:4",
            "verified": True,
            "jp_release_version": "PiNK",
            "jp_release_date": "2016-01-01",
            "source_url": "https://silentblue.remywiki.com/maimai:PiNK",
            "evidence": "Japanese chart row",
            "evidence_file": evidence.name,
            "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        }
        atomic_json(
            self.stage / "remy_conflicts.json",
            {
                "conflicts": [
                    {
                        "chart_key": "2:standard:4",
                        "reason": "conflicting_debut_evidence",
                        "candidates": [row],
                    }
                ]
            },
        )
        build(self.stage)
        validate(self.stage)
        evidence.write_text("tampered evidence")
        with self.assertRaisesRegex(ValueError, "page evidence hash"):
            validate(self.stage)

    def test_constant_diff_and_returning_chart(self):
        old = copy.deepcopy(self.old)
        old["songs"][0]["difficulties"]["standard"][0]["upstream_status"] = "missing"
        current = document([song(1, 13.3), song(2, remaster=True)])
        effective, diff = merge_and_diff(current, old, "time")
        self.assertEqual(diff["counts"]["constant_changes"], 1)
        self.assertEqual(diff["counts"]["returned_charts"], 1)
        self.assertEqual(
            effective["songs"][0]["difficulties"]["standard"][0]["upstream_status"], "present"
        )
        self.assertIsNone(jp_version_name(27500))

    def catalog(self):
        return json.loads((self.stage / "catalog.json").read_text())

    def rewrite_catalog(self, value):
        # Recompute ordinary artifact hashes so authority checks, not hash checks,
        # catch a semantically invalid but internally consistent snapshot.
        atomic_json(self.stage / "catalog.json", value)
        write_sqlite(self.stage / "catalog.sqlite", value)
        report = json.loads((self.stage / "build_report.json").read_text())
        report["outputs"] = [
            {
                "path": name,
                "sha256": hashlib.sha256((self.stage / name).read_bytes()).hexdigest(),
                "bytes": (self.stage / name).stat().st_size,
            }
            for name in ("catalog.json", "catalog.sqlite")
        ]
        atomic_json(self.stage / "build_report.json", report)

    def test_policy_metadata_is_recorded_and_legacy_metadata_stays_readable(self):
        self.prepare()
        report = build(self.stage)
        value = self.catalog()
        self.assertEqual(report["source_policy"], source_policy())
        self.assertEqual(value["source_policy"], source_policy())
        value.pop("source_policy")
        self.rewrite_catalog(value)
        self.assertEqual(validate(self.stage)["status"], "ok")

    def test_remy_only_verifies_standard_dx_remaster_not_ordinary_or_utage(self):
        utage = song(3)
        chart = utage["difficulties"].pop("standard")[0]
        chart.update(type="utage", difficulty=4, level="13?", level_value=0)
        utage["difficulties"]["utage"] = [chart]
        self.responses["lxns_songs.json"] = document([song(1), song(2, remaster=True), utage])
        keys = ["1:standard:3", "2:standard:4", "3:utage:4"]
        records = [
            {
                "chart_key": key,
                "verified": True,
                "jp_release_version": "PiNK",
                "jp_release_date": "2016-01-01",
                "source_url": "https://silentblue.remywiki.com/maimai:PiNK",
                "evidence": "Japanese dated chart row",
            }
            for key in keys
        ]
        atomic_json(self.stage / "remy_release_overrides.json", {"overrides": records})
        self.prepare()
        report = build(self.stage)
        charts = {c["chart_key"]: c for s in self.catalog()["songs"] for c in s["charts"]}
        self.assertEqual(charts[keys[1]]["release_status"], "verified")
        for key in (keys[0], keys[2]):
            self.assertIsNone(charts[key]["jp_release_version"])
            self.assertIsNone(charts[key]["jp_release_date"])
        self.assertEqual(report["counts"]["remaster_verified"], 1)
        self.assertEqual(
            sum(r["kind"] == "release_override_outside_remaster_scope" for r in report["issues"]), 2
        )
        validate(self.stage)

    def test_sega_and_waterfish_versions_never_fill_any_jp_release(self):
        self.responses["sega_jp_songs.json"] = [
            {"title": f"Song {sid}", "artist": "SEGA artist", "version": 23000, "lev_bas": "1"}
            for sid in (1, 2)
        ]
        self.responses["divingfish_music_data.json"] = [
            {
                "id": str(sid),
                "title": f"Other title {sid}",
                "type": "SD",
                "basic_info": {"artist": "Waterfish artist", "from": "Waterfish CN version"},
                "ds": [1, 2, 3, 14.9, 15],
            }
            for sid in (1, 2)
        ]
        self.responses["lxns_aliases.json"] = {
            "aliases": [{"song_id": 1, "aliases": ["LXNS alias"]}]
        }
        self.prepare()
        build(self.stage)
        songs = self.catalog()["songs"]
        ordinary, remaster = (row["charts"][0] for row in songs)
        self.assertIsNone(ordinary["jp_release_version_candidate"])
        self.assertIsNone(ordinary["jp_release_version"])
        self.assertIsNone(ordinary["type_release_version_candidate"])
        self.assertIsNone(ordinary["type_release_version_region"])
        self.assertEqual(ordinary["constant"], 13.0)
        self.assertEqual(songs[0]["title"], "Song 1")
        self.assertEqual(songs[0]["artist"], "Artist")
        self.assertEqual(songs[0]["aliases"], ["LXNS alias"])
        self.assertEqual(remaster["release_status"], "unresolved")
        self.assertIsNone(remaster["jp_release_version_candidate"])
        self.assertIsNone(remaster["jp_release_version"])
        validate(self.stage)

    def test_sega_is_disabled_for_single_or_dual_chart_type(self):
        for cn_dual, sega_dual in ((True, False), (False, True), (True, True)):
            with self.subTest(cn_dual=cn_dual, sega_dual=sega_dual):
                current = song(1)
                if cn_dual:
                    chart = copy.deepcopy(current["difficulties"]["standard"][0])
                    chart["type"] = "dx"
                    current["difficulties"]["dx"] = [chart]
                self.responses["lxns_songs.json"] = document([current, song(2, remaster=True)])
                record = {"title": "Song 1", "artist": "Artist", "version": 23000, "lev_bas": "1"}
                if sega_dual:
                    record["dx_lev_bas"] = "1"
                self.responses["sega_jp_songs.json"] = [record]
                self.prepare()
                build(self.stage)
                for chart in self.catalog()["songs"][0]["charts"]:
                    self.assertIsNone(chart["jp_release_version_candidate"])
                validate(self.stage)

    def test_non_remy_override_or_missing_date_stays_pending(self):
        for source_url, date in (
            ("https://maimai.sega.jp/example", "2016-01-01"),
            ("https://www.diving-fish.com/example", "2016-01-01"),
            ("https://silentblue.remywiki.com/maimai:PiNK", None),
        ):
            with self.subTest(source_url=source_url, date=date):
                record = {
                    "chart_key": "2:standard:4",
                    "verified": True,
                    "jp_release_version": "PiNK",
                    "jp_release_date": date,
                    "source_url": source_url,
                    "evidence": "A row",
                }
                atomic_json(self.stage / "remy_release_overrides.json", {"overrides": [record]})
                self.prepare()
                build(self.stage)
                self.assertEqual(validate(self.stage)["remaster_unresolved"], 1)

    def test_validator_rejects_wrong_authority_even_with_consistent_hashes(self):
        self.prepare()
        build(self.stage)
        original = self.catalog()
        for field, value, message in (
            ("jp_release_version_candidate", "PiNK", "legacy CN/SEGA version fallbacks"),
            ("type_release_version_region", "JP", "legacy CN/SEGA version fallbacks"),
        ):
            with self.subTest(field=field):
                modified = copy.deepcopy(original)
                modified["songs"][1]["charts"][0][field] = value
                self.rewrite_catalog(modified)
                with self.assertRaisesRegex(ValueError, message):
                    validate(self.stage)
        modified = copy.deepcopy(original)
        modified["songs"][0]["aliases"] = ["not from LXNS"]
        self.rewrite_catalog(modified)
        with self.assertRaisesRegex(ValueError, "LXNS alias authority"):
            validate(self.stage)
        modified = copy.deepcopy(original)
        modified["source_policy"]["waterfish"]["jp_release_authority"] = True
        self.rewrite_catalog(modified)
        with self.assertRaisesRegex(ValueError, "source policy"):
            validate(self.stage)

    def test_arcade_receipt_previous_snapshot_and_resolved_review_are_built(self):
        from tests.test_catalog_versions import arcade

        source = arcade(title="Song 1", version="maimaiでらっくす")
        self.responses["arcade_songs.json"] = source
        atomic_json(self.previous / "catalog.json", {"songs": [], "marker": "unchanged prior"})
        prep = self.prepare()
        self.assertEqual(prep["sources"][-1]["file"], "arcade_songs.json")
        self.assertEqual(prep["sources"][-1]["fetch_status"], "fresh")
        self.assertEqual(
            json.loads((self.stage / "previous_catalog.json").read_text()),
            {"songs": [], "marker": "unchanged prior"},
        )
        report = build(self.stage)
        self.assertEqual(self.catalog()["songs"][0]["charts"][0]["jp_release_version"], "DX")
        self.assertEqual(
            self.catalog()["songs"][0]["charts"][0]["jp_release_provider"], "arcade_songs"
        )
        self.assertEqual(report["decision_revision"], 0)
        self.assertTrue((self.stage / "catalog_review.json").is_file())
        validate(self.stage)
        self.assertTrue(all("sega.jp" not in url for url in self.calls))

    def test_arcade_empty_fresh_response_reuses_cache(self):
        from tests.test_catalog_versions import arcade

        cached = arcade(title="Song 1")
        atomic_json(self.previous / "raw/arcade_songs.json", cached)
        report = self.prepare()
        self.assertEqual(report["sources"][-1]["fetch_status"], "stale")
        self.assertEqual(json.loads((self.stage / "raw/arcade_songs.json").read_text()), cached)
        self.assertTrue(any(row["file"] == "arcade_songs.json" for row in report["warnings"]))

    def test_arcade_structure_and_duplicate_checks_allow_historical_and_utage(self):
        from tests.test_catalog_versions import arcade

        good = arcade()
        good["songs"][0]["sheets"][0].update(version=None, regions={"jp": False})
        good["songs"][0]["sheets"].append(
            {"type": "utage", "difficulty": "【逆】", "version": None}
        )
        self.assertEqual(check_source("arcade_songs.json", good), 1)
        bad_cases = [{"songs": []}, {"songs": good["songs"] * 2}]
        for field, value in (
            ("type", "invalid"),
            ("difficulty", "unknown"),
            ("version", []),
            ("version", {}),
        ):
            bad = copy.deepcopy(good)
            bad["songs"][0]["sheets"][0][field] = value
            bad_cases.append(bad)
        bad = copy.deepcopy(good)
        bad["songs"][0]["sheets"].append(copy.deepcopy(bad["songs"][0]["sheets"][0]))
        bad_cases.append(bad)
        for bad in bad_cases:
            with self.subTest(value=bad), self.assertRaises(ValueError):
                check_source("arcade_songs.json", bad)

    def record(self, sid=1, **kwargs):
        return next(
            r for r in load_record_review(self.stage, **kwargs)["records"] if r["song_id"] == sid
        )

    def with_comparable_sources(self):
        from tests.test_catalog_versions import arcade

        self.responses["arcade_songs.json"] = arcade(title="Song 1")
        self.responses["divingfish_music_data.json"] = [
            {
                "id": "1",
                "title": "Song 1",
                "type": "SD",
                "ds": [1, 2, 3, 14.4],
                "level": ["1", "2", "3", "14"],
                "basic_info": {
                    "title": "Song 1",
                    "artist": "Other Artist",
                    "genre": "舞萌",
                    "bpm": 120.0,
                    "from": "CN only",
                },
                "charts": [{"notes": [1, 0, 0, 0], "charter": "-"} for _ in range(4)],
            }
        ]
        self.prepare()
        build(self.stage)

    def choose_field(self, field_key, source="divingfish", sid=1):
        review = load_record_review(self.stage)
        field = next(
            f
            for r in review["records"]
            if r["song_id"] == sid
            for f in r["fields"]
            if f["field_key"] == field_key
        )
        option = next(o for o in field["options"] if o["source"] == source)
        return validate_field_decision(
            review, sid, {"field_key": field_key, "option_id": option["option_id"]}
        )

    def test_record_comparison_numeric_genre_missing_designer_and_region_boundaries(self):
        from tests.test_catalog_versions import arcade

        original = song()
        original["difficulties"]["standard"][0]["note_designer"] = ""
        fish = [
            {
                "id": "1",
                "title": original["title"],
                "type": "SD",
                "ds": [1, 2, 3, 13.0],
                "level": ["1", "2", "3", "13"],
                "basic_info": {"artist": original["artist"], "bpm": 120.0, "genre": "舞萌"},
                "charts": [{"charter": "-", "notes": [1, 0, 0, 0]} for _ in range(4)],
            }
        ]
        source = arcade(title=original["title"])
        source["songs"][0].update(bpm=120.0, category="maimai")
        for sheet in source["songs"][0]["sheets"]:
            sheet.update(
                level="15",
                internalLevelValue=15.0,
                noteDesigner="-",
                noteCounts={"tap": 1, "hold": 0, "slide": 0, "touch": None, "break": 0, "total": 1},
            )
        review = build_record_review(document([original]), fish, source)
        fields = {f["field_key"]: f for f in review["records"][0]["fields"]}
        for key in (
            "song.bpm",
            "song.genre",
            "chart:1:standard:3:constant",
            "chart:1:standard:3:notes",
            "chart:1:standard:3:note_designer",
        ):
            self.assertFalse(fields[key]["has_difference"], key)
        self.assertEqual(
            {o["source"] for o in fields["chart:1:standard:3:constant"]["options"]},
            {"lxns", "divingfish"},
        )
        self.assertNotIn("version", fields)
        source["songs"][0]["sheets"][3]["noteDesigner"] = "Known Designer"
        changed = build_record_review(document([original]), fish, source)
        designer = next(
            f for f in changed["records"][0]["fields"] if f["field_key"].endswith(":note_designer")
        )
        self.assertTrue(designer["has_difference"])

    def link_records(self, *, unmatched=False):
        from tests.test_catalog_versions import arcade

        first, second = song(131), song(383)
        first.update(title="Link", artist="Clean Tears feat. Youna", bpm=132, genre="maimai")
        second.update(
            title="Link", artist="Circle of friends", bpm=198, genre="niconicoボーカロイド"
        )
        raw = {"songs": []}
        for original, external_id, genre in (
            (first, "Link", "maimai"),
            (second, "Link (2)", "niconico＆ボーカロイド"),
        ):
            row = arcade(title="Link", artist=original["artist"], external_id=external_id)["songs"][
                0
            ]
            row.update(bpm=original["bpm"], category=genre)
            for sheet in row["sheets"]:
                sheet.update(
                    noteDesigner="Designer",
                    noteCounts=copy.deepcopy(original["difficulties"]["standard"][0]["notes"]),
                )
            raw["songs"].append(row)
        if unmatched:
            first["artist"] = "Unmatched Author"
        return document([first, second]), raw

    def test_link_131_and_383_only_compare_their_own_arcade_identity(self):
        original, raw = self.link_records()
        versions = resolve_versions(original, raw, [])
        report = build_record_review(original, [], raw, version_review=versions)
        for record, wanted, excluded in (
            (report["records"][0], "Link", "Link (2)"),
            (report["records"][1], "Link (2)", "Link"),
        ):
            with self.subTest(song_id=record["song_id"]):
                self.assertFalse(any(f["has_difference"] for f in record["fields"]))
                for field in record["fields"]:
                    if field["semantic_domain"] != "jp_release":
                        ids = {
                            o["external_id"]
                            for o in field["options"]
                            if o["source"] == "arcade_songs"
                        }
                        self.assertTrue(ids <= {wanted})
                        if field["field_key"] == "song.artist":
                            self.assertEqual(ids, {wanted})
                audit = {
                    r["external_id"]: r
                    for r in record["source_records"]
                    if r["source"] == "arcade_songs"
                }
                self.assertTrue(audit[wanted]["comparison_eligible"])
                self.assertFalse(audit[excluded]["comparison_eligible"])
                self.assertEqual(audit[excluded]["record"]["title"], "Link")

    def test_record_identity_select_reject_revoke_and_same_batch_field_choice(self):
        original, raw = self.link_records(unmatched=True)
        original["songs"] = original["songs"][:1]
        before_versions = resolve_versions(original, raw, [])
        before = build_record_review(original, [], raw, version_review=before_versions)
        artist = next(f for f in before["records"][0]["fields"] if f["field_key"] == "song.artist")
        choice = next(
            o
            for o in artist["options"]
            if o["source"] == "arcade_songs" and o["external_id"] == "Link (2)"
        )
        field_decision = validate_field_decision(
            before, 131, {"field_key": "song.artist", "option_id": choice["option_id"]}
        )
        candidate = next(
            c for c in before_versions["items"][0]["candidates"] if c["external_id"] == "Link (2)"
        )
        identity = validate_decision(
            before_versions,
            {
                "object_key": "131:standard:ordinary",
                "kind": "identity",
                "action": "select",
                "candidate_id": candidate["candidate_id"],
            },
        )
        decisions = {"revision": 1, "decisions": [field_decision, identity]}
        after_versions = resolve_versions(original, raw, [], adjudications=decisions)
        after = build_record_review(
            original, [], raw, version_review=after_versions, adjudications=decisions
        )
        after_artist = next(
            f for f in after["records"][0]["fields"] if f["field_key"] == "song.artist"
        )
        self.assertEqual(
            {o["external_id"] for o in after_artist["options"] if o["source"] == "arcade_songs"},
            {"Link (2)"},
        )
        # Filtering the UI after this same transaction does not invalidate unchanged raw evidence.
        self.assertEqual(
            validate_field_decision(after, 131, field_decision)["evidence_context_fingerprint"],
            field_decision["evidence_context_fingerprint"],
        )
        base = copy.deepcopy(original["songs"][0])
        base["charts"] = [
            {**c, "chart_key": "131:standard:3", "constant": c["level_value"]}
            for c in base["difficulties"]["standard"]
        ]
        applied = apply_field_decisions([base], after, decisions)
        self.assertEqual(applied["songs"][0]["artist"], "Circle of friends")
        self.assertEqual(
            next(
                f
                for f in applied["review"]["records"][0]["fields"]
                if f["field_key"] == "song.artist"
            )["status"],
            "manual",
        )
        rejected = validate_decision(before_versions, {**identity, "action": "reject"})
        rejected_versions = resolve_versions(
            original, raw, [], adjudications={"decisions": [rejected]}
        )
        rejected_report = build_record_review(original, [], raw, version_review=rejected_versions)
        rejected_artist = next(
            f for f in rejected_report["records"][0]["fields"] if f["field_key"] == "song.artist"
        )
        self.assertEqual(
            {o["external_id"] for o in rejected_artist["options"] if o["source"] == "arcade_songs"},
            {"Link"},
        )
        denied = apply_field_decisions(
            [base], rejected_report, {"decisions": [field_decision, rejected]}
        )
        self.assertEqual(denied["songs"][0]["artist"], "Unmatched Author")
        denied_field = next(
            f for f in denied["review"]["records"][0]["fields"] if f["field_key"] == "song.artist"
        )
        self.assertEqual(denied_field["status"], "conflict")
        self.assertEqual(denied_field["decision"], field_decision)
        revoked = build_record_review(
            original, [], raw, version_review=resolve_versions(original, raw, [])
        )
        revoked_artist = next(
            f for f in revoked["records"][0]["fields"] if f["field_key"] == "song.artist"
        )
        self.assertEqual(
            {o["external_id"] for o in revoked_artist["options"] if o["source"] == "arcade_songs"},
            {"Link", "Link (2)"},
        )

    def test_ambiguous_exact_arcade_records_do_not_reintroduce_other_authors(self):
        original, raw = self.link_records()
        original["songs"] = original["songs"][:1]
        duplicate = copy.deepcopy(raw["songs"][0])
        duplicate["songId"] = "Link exact duplicate"
        raw["songs"].append(duplicate)
        report = build_record_review(
            original, [], raw, version_review=resolve_versions(original, raw, [])
        )
        artist = next(f for f in report["records"][0]["fields"] if f["field_key"] == "song.artist")
        self.assertEqual(
            {o["external_id"] for o in artist["options"] if o["source"] == "arcade_songs"},
            {"Link", "Link exact duplicate"},
        )

    def test_field_choices_overlay_and_author_does_not_change_jp_identity_next_build(self):
        self.with_comparable_sources()
        choices = [
            self.choose_field("song.artist"),
            self.choose_field("chart:1:standard:3:constant"),
        ]
        atomic_json(self.stage / "previous_catalog.json", self.catalog())
        atomic_json(
            self.stage / "catalog_adjudications.json", {"revision": 1, "decisions": choices}
        )
        build(self.stage)
        validate(self.stage)
        current = self.catalog()["songs"][0]
        self.assertEqual(current["artist"], "Other Artist")
        self.assertEqual(current["source_identity"]["artist"], "Artist")
        chart = current["charts"][0]
        self.assertEqual(chart["constant"], 14.4)
        self.assertEqual(chart["constant_source"], "manual_divingfish")
        self.assertEqual(chart["jp_release_version"], "ORANGE")
        atomic_json(self.stage / "previous_catalog.json", self.catalog())
        build(self.stage)
        validate(self.stage)
        self.assertEqual(self.catalog()["songs"][0]["charts"][0]["release_status"], "resolved")
        self.assertNotIn("object_identity_changed", self.record()["identities"][0]["reasons"])

    def test_field_selection_binds_identity_and_evidence_then_revoke_restores_lxns(self):
        self.with_comparable_sources()
        choice = self.choose_field("song.artist")
        atomic_json(
            self.stage / "catalog_adjudications.json", {"revision": 1, "decisions": [choice]}
        )
        build(self.stage)
        changed = json.loads((self.stage / "raw/divingfish_music_data.json").read_text())
        changed[0]["basic_info"]["artist"] = "Changed Artist"
        atomic_json(self.stage / "raw/divingfish_music_data.json", changed)
        receipts = json.loads((self.stage / "raw/sources.json").read_text())
        content = (self.stage / "raw/divingfish_music_data.json").read_bytes()
        for row in receipts:
            if row["file"] == "divingfish_music_data.json":
                row.update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
        atomic_json(self.stage / "raw/sources.json", receipts)
        build(self.stage)
        validate(self.stage)
        field = next(f for f in self.record()["fields"] if f["field_key"] == "song.artist")
        self.assertEqual(field["status"], "conflict")
        self.assertIn("decision_stale", field["reasons"])
        self.assertEqual(field["decision"]["value"], "Other Artist")
        self.assertEqual(self.catalog()["songs"][0]["artist"], "Artist")
        atomic_json(self.stage / "catalog_adjudications.json", {"revision": 2, "decisions": []})
        build(self.stage)
        self.assertIsNone(
            next(f for f in self.record()["fields"] if f["field_key"] == "song.artist")["decision"]
        )

    def test_unpublished_field_decision_never_changes_current(self):
        self.with_comparable_sources()
        choice = self.choose_field("song.artist")
        record = self.record(adjudications={"revision": 1, "decisions": [choice]})
        field = next(f for f in record["fields"] if f["field_key"] == "song.artist")
        self.assertEqual(field["current"]["value"], "Artist")
        self.assertTrue(field["decision_pending"])
        self.assertEqual(field["requested_value"], "Other Artist")
        self.assertEqual(self.catalog()["songs"][0]["artist"], "Artist")

    def test_orphan_field_decisions_remain_visible_and_revocable(self):
        self.with_comparable_sources()
        decision = self.choose_field("chart:1:standard:3:constant")
        missing = copy.deepcopy(decision)
        missing.update(
            song_id=999,
            field_key="chart:999:standard:3:constant",
            object_key="field:999:chart:999:standard:3:constant",
        )
        active = {"revision": 1, "decisions": [missing]}
        report = load_record_review(self.stage, adjudications=active)
        row = next(r for r in report["records"] if r["song_id"] == 999)
        self.assertTrue(row["unavailable"])
        self.assertEqual(row["fields"][0]["status"], "conflict")
        self.assertIn("object_missing", row["fields"][0]["reasons"])
        revoke = validate_field_decision(
            report, 999, {"field_key": missing["field_key"], "action": "revoke"}
        )
        self.assertEqual(revoke["object_key"], missing["object_key"])
        self.assertEqual(revoke["action"], "revoke")

    def test_field_selector_rejects_cross_domain_and_mismatched_fingerprint(self):
        self.with_comparable_sources()
        review = load_record_review(self.stage)
        field = next(f for f in review["records"][0]["fields"] if f["field_key"] == "song.artist")
        with self.assertRaisesRegex(ValueError, "证据"):
            validate_field_decision(
                review,
                1,
                {
                    "field_key": "song.artist",
                    "option_id": field["options"][1]["option_id"],
                    "object_fingerprint": "wrong",
                },
            )
        version = next(
            f for f in review["records"][0]["fields"] if f["semantic_domain"] == "jp_release"
        )
        with self.assertRaisesRegex(ValueError, "语义域"):
            validate_field_decision(
                review,
                1,
                {
                    "field_key": version["field_key"],
                    "option_id": version["options"][0]["option_id"],
                },
            )

    def test_invalid_notes_and_nonfinite_constant_rejected(self):
        invalid = document([song()])
        invalid["songs"][0]["difficulties"]["standard"][0]["notes"]["total"] = 2
        with self.assertRaisesRegex(ValueError, "notes"):
            check_source("lxns_songs.json", invalid)
        invalid = document([song(constant=float("nan"))])
        with self.assertRaisesRegex(ValueError, "level"):
            check_source("lxns_songs.json", invalid)

    def test_reject_empty_or_duplicate_authority(self):
        with self.assertRaises(ValueError):
            check_source("lxns_songs.json", document([]))
        with self.assertRaises(ValueError):
            check_source("lxns_songs.json", document([song(), song()]))


if __name__ == "__main__":
    unittest.main()
