import copy
import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from scripts.build_catalog import build, search
from scripts.catalog_chart_compiler import (
    chart_mappings,
    load_record_review,
    migrate_decisions,
    validate_chart_decisions,
)
from scripts.catalog_pipeline import atomic_json
from scripts.validate_catalog import validate
from tests import test_catalog_pipeline as legacy_fixtures

song, document = legacy_fixtures.song, legacy_fixtures.document


class ChartCompilerTests(unittest.TestCase):
    setUp = legacy_fixtures.PipelineTests.setUp
    fetch = legacy_fixtures.PipelineTests.fetch
    prepare = legacy_fixtures.PipelineTests.prepare
    with_comparable_sources = legacy_fixtures.PipelineTests.with_comparable_sources

    def read(self, name):
        return json.loads((self.stage / name).read_text())

    def compile(self):
        build(self.stage)
        return validate(self.stage)

    def rehash_source(self, name, value):
        atomic_json(self.stage / "raw" / name, value)
        receipts = self.read("raw/sources.json")
        content = (self.stage / "raw" / name).read_bytes()
        for row in receipts:
            if row["file"] == name:
                row.update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
        atomic_json(self.stage / "raw/sources.json", receipts)

    def choices(self, ident=1, difficulty=3):
        review = load_record_review(self.stage)
        row = next(
            r for r in review["records"] if (r["id"], r["difficulty"]) == (ident, difficulty)
        )
        selected = [
            {
                "field_key": f["field_key"],
                "option_id": next(
                    (o for o in f["options"] if o["source"] == "divingfish"), f["options"][0]
                )["option_id"],
            }
            for f in row["fields"]
        ]
        return validate_chart_decisions(review, ident, difficulty, selected)

    def test_flat_identity_and_missing_remy_does_not_gate(self):
        self.prepare()
        result = self.compile()
        catalog = self.read("catalog.json")
        self.assertEqual(catalog["schema_version"], 2)
        self.assertNotIn("songs", catalog)
        self.assertEqual([c["chart_key"] for c in catalog["charts"]], ["1:3", "2:4"])
        self.assertEqual(result["pending_charts"], 0)
        self.assertTrue(
            next(c for c in catalog["charts"] if c["difficulty"] == 4)["version_pending"]
        )
        self.assertTrue(all(c["cn_version"] == c["api_version"] for c in catalog["charts"]))
        self.assertTrue(
            all("level_value" not in c and "version" not in c for c in catalog["charts"])
        )
        self.assertEqual(search(self.stage, "2", id_only=True)[0]["chart_key"], "2:4")
        with closing(sqlite3.connect(self.stage / "catalog.sqlite")) as conn:
            self.assertEqual(
                [(r[1], r[5]) for r in conn.execute("PRAGMA table_info(charts)") if r[5]],
                [("id", 1), ("difficulty", 2)],
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='songs'"
                ).fetchone()
            )

    def test_real_conflicts_gate_entire_chart_and_complete_choices_are_required(self):
        self.with_comparable_sources()
        result = self.compile()
        self.assertEqual(result["pending_charts"], 1)
        self.assertEqual([c["chart_key"] for c in self.read("catalog.json")["charts"]], ["2:4"])
        review = load_record_review(self.stage)
        pending = next(r for r in review["records"] if r["status"] == "pending")
        self.assertEqual(
            {f["field_key"] for f in pending["fields"]}, {"artist", "constant", "level"}
        )
        self.assertTrue(
            all("current" not in f and "recommended_option_id" not in f for f in pending["fields"])
        )
        first = pending["fields"][0]
        with self.assertRaisesRegex(ValueError, "全部"):
            validate_chart_decisions(
                review,
                1,
                3,
                [{"field_key": first["field_key"], "option_id": first["options"][0]["option_id"]}],
            )
        with closing(sqlite3.connect(self.stage / "catalog.sqlite")) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM charts WHERE id=1").fetchone())

    def test_selected_values_permanent_across_source_changes_removal_and_rollback(self):
        self.with_comparable_sources()
        self.compile()
        decisions = self.choices()
        atomic_json(
            self.stage / "catalog_adjudications.json",
            {"schema_version": 2, "revision": 1, "decisions": decisions},
        )
        self.compile()
        chart = next(c for c in self.read("catalog.json")["charts"] if c["id"] == 1)
        self.assertEqual(
            (chart["artist"], chart["constant"], chart["level"]), ("Other Artist", 14.4, "14")
        )
        self.assertNotIn("level_value", chart)
        original = self.read("raw/divingfish_music_data.json")
        changed = copy.deepcopy(original)
        changed[0]["basic_info"]["artist"] = "Completely New Artist"
        changed[0]["ds"][3] = 15.0
        self.rehash_source("divingfish_music_data.json", changed)
        self.compile()
        self.rehash_source("divingfish_music_data.json", [])
        self.compile()
        self.rehash_source("divingfish_music_data.json", original)
        self.compile()
        chart = next(c for c in self.read("catalog.json")["charts"] if c["id"] == 1)
        self.assertEqual(
            (chart["artist"], chart["constant"], chart["level"]), ("Other Artist", 14.4, "14")
        )
        self.assertFalse(
            next(r for r in self.read("catalog_record_review.json")["records"] if r["id"] == 1)[
                "fields"
            ]
        )
        atomic_json(
            self.stage / "catalog_adjudications.json",
            {"schema_version": 2, "revision": 2, "decisions": []},
        )
        self.compile()
        self.assertNotIn(1, {c["id"] for c in self.read("catalog.json")["charts"]})

    def test_new_different_field_conflict_gates_without_reopening_decided_fields(self):
        self.with_comparable_sources()
        self.compile()
        atomic_json(
            self.stage / "catalog_adjudications.json",
            {"schema_version": 2, "revision": 1, "decisions": self.choices()},
        )
        self.compile()
        source = self.read("raw/divingfish_music_data.json")
        source[0]["basic_info"].update(artist="A changed again", bpm=160)
        self.rehash_source("divingfish_music_data.json", source)
        self.compile()
        row = next(r for r in self.read("catalog_record_review.json")["records"] if r["id"] == 1)
        self.assertEqual([f["field_key"] for f in row["fields"]], ["bpm"])
        self.assertEqual(
            next(d["value"] for d in row["decisions"] if d["field_key"] == "artist"), "Other Artist"
        )

    def test_saved_not_published_does_not_report_admitted(self):
        self.with_comparable_sources()
        self.compile()
        active = {"schema_version": 2, "revision": 1, "decisions": self.choices()}
        report = load_record_review(self.stage, active)
        self.assertEqual(report["counts"]["pending"], 1)
        row = next(r for r in report["records"] if r["id"] == 1)
        self.assertEqual(row["status"], "pending")
        self.assertTrue(row["decision_pending"])
        with self.assertRaisesRegex(ValueError, "等待发布"):
            validate_chart_decisions(report, 1, 3, [])

    def test_dx_id_only_adds_offset_and_utage_keeps_original(self):
        current = song(1)
        current["difficulties"]["dx"] = [{**current["difficulties"]["standard"][0], "type": "dx"}]
        utage = song(100001)
        utage["difficulties"] = {
            "utage": [{**utage["difficulties"]["standard"][0], "type": "utage", "difficulty": 0}]
        }
        result = chart_mappings(document([current, utage]))
        self.assertEqual(
            {r["chart_key"] for r in result["mappings"]}, {"1:3", "10001:3", "100001:0"}
        )
        self.assertEqual(
            next(r for r in result["mappings"] if r["id"] == 10001)["legacy_chart_key"], "1:dx:3"
        )

    def test_migration_expands_song_choice_and_missing_reject_aborts(self):
        self.with_comparable_sources()
        old = {
            "revision": 4,
            "decisions": [
                {
                    "kind": "field",
                    "object_key": "field:1:song.artist",
                    "song_id": 1,
                    "field_key": "song.artist",
                    "action": "select",
                    "source": "divingfish",
                    "value": "Permanent Author",
                }
            ],
        }
        converted = migrate_decisions(old, self.stage)
        self.assertEqual(converted["revision"], 4)
        self.assertEqual(converted["schema_version"], 2)
        self.assertEqual(converted["decisions"][0]["object_key"], "chart:1:3:artist")
        self.assertEqual(converted["decisions"][0]["value"], "Permanent Author")
        with self.assertRaisesRegex(ValueError, "空版本"):
            migrate_decisions(
                {
                    "decisions": [
                        {"kind": "version", "object_key": "2:standard:4", "action": "reject"}
                    ]
                },
                self.stage,
            )

    def test_sql_materialized_column_tampering_is_rejected_even_with_new_hash(self):
        self.prepare()
        self.compile()
        with closing(sqlite3.connect(self.stage / "catalog.sqlite")) as conn:
            conn.execute("UPDATE charts SET constant=99 WHERE id=1")
            conn.commit()
        report = self.read("build_report.json")
        for row in report["outputs"]:
            if row["path"] == "catalog.sqlite":
                row.update(
                    sha256=hashlib.sha256((self.stage / "catalog.sqlite").read_bytes()).hexdigest(),
                    bytes=(self.stage / "catalog.sqlite").stat().st_size,
                )
        atomic_json(self.stage / "build_report.json", report)
        with self.assertRaisesRegex(ValueError, "SQL column mismatch"):
            validate(self.stage)

    def test_remy_protects_durable_white_value_even_when_chart_not_admitted(self):
        from scripts.remy_release import run

        self.prepare()
        atomic_json(self.stage / "catalog.json", {"schema_version": 2, "charts": []})
        atomic_json(
            self.stage / "catalog_adjudications.json",
            {
                "schema_version": 2,
                "revision": 1,
                "decisions": [
                    {
                        "object_key": "chart:2:4:jp_release_version",
                        "kind": "chart_field",
                        "id": 2,
                        "difficulty": 4,
                        "field_key": "jp_release_version",
                        "action": "select",
                        "value": "PiNK",
                        "source": "remywiki",
                        "date": "2016-01-01",
                    }
                ],
            },
        )
        with patch(
            "scripts.remy_release.resolve", return_value={"pending": [], "additions": []}
        ) as resolve:
            run(self.stage)
        protected = resolve.call_args.kwargs["protected_records"]
        self.assertTrue(
            any(
                r["chart_key"] == "2:standard:4"
                and r["jp_release_provider"] == "manual"
                and r["jp_release_version"] == "PiNK"
                for r in protected
            )
        )


if __name__ == "__main__":
    unittest.main()
