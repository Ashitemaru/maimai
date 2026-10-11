"""A review field represents competing values, not an adapter status flag."""

import copy
import hashlib
import json
import unittest

from scripts.build_catalog import build
from scripts.catalog_chart_compiler import load_record_review, validate_chart_decisions
from scripts.catalog_pipeline import atomic_json
from scripts.catalog_versions import resolve_field_versions
from scripts.validate_catalog import validate
from tests import test_catalog_pipeline as fixtures
from tests.test_catalog_versions import arcade


class CatalogValueConflictTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    fetch = fixtures.PipelineTests.fetch
    prepare = fixtures.PipelineTests.prepare

    def read(self, name):
        return json.loads((self.stage / name).read_text())

    def compile(self):
        build(self.stage)
        self.assertEqual(validate(self.stage)["status"], "ok")
        return self.read("catalog.json")

    def row(self, ident=1, difficulty=3):
        return next(
            row
            for row in load_record_review(self.stage)["records"]
            if (row["id"], row["difficulty"]) == (ident, difficulty)
        )

    def prepare_arcade(self, *records, artist="Artist"):
        self.responses["lxns_songs.json"]["songs"][0]["artist"] = artist
        self.responses["arcade_songs.json"] = {"songs": list(records)}
        self.prepare()

    def replace_arcade(self, *records):
        name = "arcade_songs.json"
        atomic_json(self.stage / "raw" / name, {"songs": list(records)})
        content = (self.stage / "raw" / name).read_bytes()
        receipts = self.read("raw/sources.json")
        for receipt in receipts:
            if receipt["file"] == name:
                receipt.update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
        atomic_json(self.stage / "raw/sources.json", receipts)

    def decide(self, field_key, value):
        review = load_record_review(self.stage)
        field = next(field for field in self.row()["fields"] if field["field_key"] == field_key)
        option = next(option for option in field["options"] if option["value"] == value)
        decisions = validate_chart_decisions(
            review,
            1,
            3,
            [
                {"field_key": field_key, "option_id": option["option_id"]},
            ],
        )
        atomic_json(
            self.stage / "catalog_adjudications.json",
            {"schema_version": 2, "revision": 1, "decisions": decisions},
        )

    def test_single_jp_value_with_arcade_author_difference_is_admitted(self):
        record = arcade(
            title="Song 1", artist="Junky feat. 乙姫(CV:浅川悠)", version="MURASAKi PLUS"
        )["songs"][0]
        self.prepare_arcade(record, artist="Junky")
        catalog = self.compile()
        self.assertEqual(self.row()["fields"], [])
        self.assertEqual(self.row()["status"], "resolved")
        chart = next(chart for chart in catalog["charts"] if chart["chart_key"] == "1:3")
        self.assertEqual(chart["artist"], "Junky")
        self.assertEqual(chart["jp_release_version"], "MURASAKi PLUS")

    def test_unique_jp_value_needs_no_author_or_version_decision(self):
        record = arcade(
            title="Song 1", artist="Junky feat. 乙姫(CV:浅川悠)", version="MURASAKi PLUS"
        )["songs"][0]
        self.prepare_arcade(record, artist="Junky")
        catalog = self.compile()
        chart = next(chart for chart in catalog["charts"] if chart["chart_key"] == "1:3")
        self.assertEqual(chart["artist"], "Junky")
        self.assertEqual(chart["jp_release_version"], "MURASAKi PLUS")
        self.assertFalse(chart["version_pending"])
        self.assertEqual(chart["field_decisions"], {})
        self.assertEqual(self.row()["fields"], [])

    def test_arcade_non_version_values_never_supply_or_conflict_with_chart_fields(self):
        record = arcade(title="SONG 1", artist="Different arcade artist", version="MURASAKi PLUS")[
            "songs"
        ][0]
        record.update(bpm=999, category="東方Project")
        for sheet in record["sheets"]:
            sheet.update(
                level="15",
                internalLevelValue=15.0,
                noteDesigner="Arcade Designer",
                noteCounts={
                    "tap": 200,
                    "hold": 100,
                    "slide": 100,
                    "touch": 0,
                    "break": 100,
                    "total": 500,
                },
            )
        self.prepare_arcade(record)
        catalog = self.compile()
        self.assertEqual(self.row()["fields"], [])
        chart = next(chart for chart in catalog["charts"] if chart["chart_key"] == "1:3")
        self.assertEqual(chart["title"], "Song 1")
        self.assertEqual(chart["artist"], "Artist")
        self.assertEqual(chart["bpm"], 120)
        self.assertEqual(chart["genre"], "maimai")
        self.assertEqual(chart["level"], "13")
        self.assertEqual(chart["constant"], 13.0)
        self.assertEqual(chart["note_designer"], "Designer")
        self.assertEqual(chart["notes"], fixtures.song()["difficulties"]["standard"][0]["notes"])
        self.assertEqual(chart["jp_release_version"], "MURASAKi PLUS")

    def test_lxns_divingfish_field_differences_still_require_choices(self):
        record = arcade(title="Song 1", artist="Ignored arcade artist", version="MURASAKi PLUS")[
            "songs"
        ][0]
        self.responses["divingfish_music_data.json"] = [
            {
                "id": "1",
                "title": "Fish title",
                "type": "SD",
                "ds": [1, 2, 3, 14.4],
                "level": ["1", "2", "3", "14"],
                "basic_info": {
                    "title": "Fish title",
                    "artist": "Fish artist",
                    "genre": "東方Project",
                    "bpm": 180,
                },
                "charts": [{"notes": [2, 1, 1, 1], "charter": "Fish Designer"} for _ in range(4)],
            }
        ]
        self.prepare_arcade(record)
        catalog = self.compile()
        fields = self.row()["fields"]
        self.assertEqual(
            {field["field_key"] for field in fields},
            {"title", "artist", "bpm", "genre", "level", "constant", "note_designer", "notes"},
        )
        for field in fields:
            self.assertEqual(
                {option["source"] for option in field["options"]}, {"lxns", "divingfish"}
            )
        self.assertNotIn("1:3", {chart["chart_key"] for chart in catalog["charts"]})

    def test_unique_divingfish_designer_fills_missing_lxns_value_without_conflict(self):
        self.responses["divingfish_music_data.json"] = [
            {
                "id": "1",
                "title": "Song 1",
                "type": "SD",
                "ds": [1, 2, 3, 13.0],
                "level": ["1", "2", "3", "13"],
                "basic_info": {"artist": "Artist", "genre": "舞萌", "bpm": 120},
                "charts": [{"notes": [1, 0, 0, 0], "charter": "Fish Designer"} for _ in range(4)],
            }
        ]
        for missing in ("", "-"):
            with self.subTest(lxns_designer=missing):
                self.responses["lxns_songs.json"]["songs"][0]["difficulties"]["standard"][0][
                    "note_designer"
                ] = missing
                self.prepare()
                catalog = self.compile()
                self.assertEqual(self.row()["fields"], [])
                chart = next(chart for chart in catalog["charts"] if chart["chart_key"] == "1:3")
                self.assertEqual(chart["note_designer"], "Fish Designer")
                self.assertEqual(chart["field_decisions"], {})

    def test_ambiguous_same_title_identities_do_not_choose_a_jp_version(self):
        first, second = fixtures.song(1), fixtures.song(2)
        first.update(title="Shared title", artist="Local A")
        second.update(title="Shared title", artist="Local B")
        source_a = arcade(
            title="Shared title", artist="Source A", version="MURASAKi PLUS", external_id="source-a"
        )["songs"][0]
        source_b = arcade(
            title="Shared title", artist="Source B", version="MiLK", external_id="source-b"
        )["songs"][0]
        cases = [([first, second], [source_a]), ([first], [source_a, source_b])]
        for local, sources in cases:
            with self.subTest(local_records=len(local), source_records=len(sources)):
                result = resolve_field_versions(fixtures.document(local), {"songs": sources}, [])
                for item in result["items"]:
                    self.assertEqual(item["candidates"], [])
                    self.assertEqual(len(item["unlinked_candidates"]), len(sources))
                    self.assertEqual(item["status"], "pending")
                    self.assertIsNone(result["objects"][item["object_key"]]["jp_release_version"])

    def test_multiple_evidence_records_with_same_version_are_not_conflict(self):
        first = arcade(title="Song 1", version="MURASAKi PLUS", external_id="first")["songs"][0]
        second = arcade(title="Song 1", version="MURASAKi PLUS", external_id="second")["songs"][0]
        self.prepare_arcade(first, second)
        catalog = self.compile()
        self.assertEqual(self.row()["fields"], [])
        self.assertEqual(self.row()["status"], "resolved")
        chart = next(chart for chart in catalog["charts"] if chart["chart_key"] == "1:3")
        self.assertEqual(chart["jp_release_version"], "MURASAKi PLUS")

    def test_distinct_current_version_values_still_require_choice(self):
        first = arcade(title="Song 1", version="MURASAKi PLUS", external_id="first")["songs"][0]
        second = arcade(title="Song 1", version="MiLK", external_id="second")["songs"][0]
        self.prepare_arcade(first, second)
        catalog = self.compile()
        fields = self.row()["fields"]
        self.assertEqual([field["field_key"] for field in fields], ["jp_release_version"])
        self.assertEqual(
            {option["value"] for option in fields[0]["options"]}, {"MURASAKi PLUS", "MiLK"}
        )
        self.assertNotIn("1:3", {chart["chart_key"] for chart in catalog["charts"]})

    def test_selected_version_remains_fixed_after_candidate_changes(self):
        first = arcade(title="Song 1", version="MURASAKi PLUS", external_id="first")["songs"][0]
        second = arcade(title="Song 1", version="MiLK", external_id="second")["songs"][0]
        self.prepare_arcade(first, second)
        self.compile()
        self.decide("jp_release_version", "MURASAKi PLUS")
        chosen = self.compile()
        atomic_json(self.stage / "previous_catalog.json", chosen)
        changed = arcade(title="Song 1", version="FiNALE", external_id="new")["songs"][0]
        self.replace_arcade(changed)
        catalog = self.compile()
        self.assertEqual(self.row()["fields"], [])
        chart = next(chart for chart in catalog["charts"] if chart["chart_key"] == "1:3")
        self.assertEqual(chart["jp_release_version"], "MURASAKi PLUS")
        self.assertEqual(chart["field_decisions"]["jp_release_version"]["value"], "MURASAKi PLUS")

    def test_single_source_changed_version_does_not_conflict_with_previous_snapshot(self):
        record = arcade(title="Song 1", version="MURASAKi PLUS")["songs"][0]
        self.prepare_arcade(record)
        previous = self.compile()
        atomic_json(self.stage / "previous_catalog.json", previous)
        changed = copy.deepcopy(record)
        for sheet in changed["sheets"]:
            sheet["version"] = "MiLK"
        self.replace_arcade(changed)
        catalog = self.compile()
        self.assertEqual(self.row()["fields"], [])
        chart = next(chart for chart in catalog["charts"] if chart["chart_key"] == "1:3")
        self.assertEqual(chart["jp_release_version"], "MiLK")
        self.assertNotEqual(chart["release_status"], "conflict")


if __name__ == "__main__":
    unittest.main()
