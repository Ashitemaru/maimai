"""Catalog build end to end: fetched sources -> review -> adjudication -> published charts."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.catalog_decisions import CatalogDecisions, StaleDecisionError
from backend.catalog_updates import CatalogUpdates
from backend.store import Store
from scripts.catalog_chart_compiler import validate_chart_decisions
from scripts.catalog_pipeline import atomic_json, prepare_update
from scripts.catalog_validate import validate
from tests.fixtures.catalog import PipelineCase, arcade, document, fish, song
from tests.support import empty_personal_database


class CatalogBuildTests(PipelineCase):
    def test_sources_build_a_validated_catalog_without_touching_previous_snapshot(self):
        current = song(1, 13.3)
        current["difficulties"]["standard"] = []  # A vanished chart stays, marked missing.
        self.responses["lxns_songs.json"] = document([current, song(3, 14)])
        before = {p: p.read_bytes() for p in self.previous.rglob("*") if p.is_file()}
        report = self.prepare()
        self.assertEqual(self.compile()["total_charts"], 3)
        self.assertEqual(report["diff"]["counts"]["added_songs"], 1)
        self.assertEqual(set(self.charts()), {"1:3", "2:4", "3:3"})
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, path)

    def test_failed_authority_fetch_keeps_inherited_sources(self):
        original = (self.stage / "raw/lxns_songs.json").read_bytes()
        self.responses["lxns_aliases.json"] = RuntimeError("denied")
        with self.assertRaisesRegex(RuntimeError, "落雪"):
            self.prepare()
        self.assertEqual((self.stage / "raw/lxns_songs.json").read_bytes(), original)

    def test_validator_rejects_tampered_raw_source(self):
        self.prepare()
        self.compile()
        atomic_json(
            self.stage / "raw/lxns_aliases.json", {"aliases": [{"song_id": 1, "aliases": []}]}
        )
        with self.assertRaisesRegex(ValueError, "raw source hash"):
            validate(self.stage)

    def test_conflict_goes_to_review_until_every_field_is_chosen(self):
        self.with_comparable_sources()
        self.assertEqual(self.compile()["pending_charts"], 1)
        self.assertEqual(list(self.charts()), ["2:4"])
        record = self.record()
        self.assertEqual(
            {f["field_key"] for f in record["fields"]}, {"artist", "constant", "level"}
        )
        first = record["fields"][0]
        partial = [{"field_key": first["field_key"], "option_id": first["options"][0]["option_id"]}]
        with self.assertRaisesRegex(ValueError, "全部"):
            validate_chart_decisions(self.review(), 1, 3, partial)
        self.adjudicate(self.choose())
        self.compile()
        chart = self.charts()["1:3"]
        self.assertEqual((chart["artist"], chart["constant"]), ("Other Artist", 14.4))

    def test_chosen_values_survive_source_changes_and_removal(self):
        self.with_comparable_sources()
        self.adjudicate(self.choose())
        self.compile()
        changed = copy.deepcopy(self.read("raw/divingfish_music_data.json"))
        changed[0]["basic_info"]["artist"] = "Completely New Artist"
        changed[0]["ds"][3] = 15.0
        for source in (changed, []):
            self.rehash_source("divingfish_music_data.json", source)
            self.compile()
            chart = self.charts()["1:3"]
            self.assertEqual((chart["artist"], chart["constant"]), ("Other Artist", 14.4))

    def test_jp_version_decision_is_permanent_when_candidates_change(self):
        first = arcade(title="Song 1", version="MURASAKi PLUS", external_id="a")["songs"][0]
        second = arcade(title="Song 1", version="MiLK", external_id="b")["songs"][0]
        self.responses["arcade_songs.json"] = {"songs": [first, second]}
        self.prepare()
        self.compile()
        self.assertNotIn("1:3", self.charts())
        self.adjudicate(self.choose(jp_release_version="MURASAKi PLUS"))
        changed = arcade(title="Song 1", version="FiNALE", external_id="c")
        self.rehash_source("arcade_songs.json", changed)
        self.compile()
        self.assertEqual(self.record()["fields"], [])
        self.assertEqual(self.charts()["1:3"]["jp_release_version"], "MURASAKi PLUS")

    def test_numeric_choice_yields_once_sources_agree(self):
        self.update_sources([song(1)], [fish(1, 14.4)])
        self.adjudicate(self.choose())
        charts, _ = self.update_sources([song(1, 13.0)], [fish(1, 14.4)])
        self.assertEqual(charts["1:3"]["constant"], 14.4)
        charts, review = self.update_sources([song(1, 13.8)], [fish(1, 13.8)])
        self.assertEqual(charts["1:3"]["constant"], 13.8)
        self.assertEqual(review[(1, 3)]["decisions"], [])

    def test_published_chart_keeps_values_while_sources_disagree(self):
        self.update_sources([song(1)], [fish(1, 13.0)])
        charts, review = self.update_sources(
            [song(1, 13.4), song(3)], [fish(1, 13.0), fish(3, 12.5)]
        )
        self.assertEqual(charts["1:3"]["constant"], 13.0)
        self.assertEqual((review[(1, 3)]["status"], review[(1, 3)]["published"]), ("pending", True))
        self.assertNotIn("3:3", charts)  # A new conflicting chart is never published.
        charts, review = self.update_sources([song(1, 13.4)], [fish(1, 13.4)])
        self.assertEqual(charts["1:3"]["constant"], 13.4)
        self.assertEqual(review[(1, 3)]["status"], "resolved")

    def test_lagging_source_defers_differences_without_review(self):
        self.update_sources([song(1)], [fish(1, 13.0)])
        charts, review = self.update_sources([song(1, 13.4), song(3)], [fish(1, 13.0)])
        self.assertEqual(charts["1:3"]["constant"], 13.0)
        self.assertEqual(review[(1, 3)]["status"], "resolved")
        self.assertIn("3:3", charts)
        self.assertEqual(self.read("catalog.json")["counts"]["source_lag"]["lxns_only"], 1)
        charts, _ = self.update_sources([song(1, 13.4), song(3)], [fish(1, 13.4), fish(3, 12.5)])
        self.assertEqual(charts["1:3"]["constant"], 13.4)

    def test_unknown_jp_version_waits_for_a_decision(self):
        self.responses["arcade_songs.json"] = arcade(title="Song 1", version="NEXT VERSION")
        self.prepare()
        self.compile()
        self.assertNotIn("1:3", self.charts())
        field = next(f for f in self.record()["fields"] if f["field_key"] == "jp_release_version")
        self.assertIn("unknown_jp_version", field["reasons"])
        self.adjudicate(self.choose())
        self.compile()
        chart = self.charts()["1:3"]
        self.assertEqual(
            (chart["jp_release_version"], chart["release_status"]), ("NEXT VERSION", "manual")
        )


class CatalogServiceTests(PipelineCase):
    """The running app's update service over a real built catalog and a real Store."""

    def setUp(self):
        super().setUp()
        self.with_comparable_sources()
        self.compile()
        runtime = empty_personal_database(self.stage.parent / "runtime")
        self.store = Store(runtime=runtime, catalog_dir=self.stage)

    def service(self, **options):
        def prepare(stage, previous, progress):
            with patch("scripts.catalog_pipeline.fetch", side_effect=self.fetch):
                return prepare_update(stage, previous, progress)

        def no_remy(*args, **kwargs):
            raise OSError("Remy is offline in tests")

        service = CatalogUpdates(self.store, prepare_fn=prepare, remy_fn=no_remy, **options)
        self.addCleanup(service._remy_pool.shutdown, wait=True)
        self.addCleanup(service._main_pool.shutdown, wait=True)
        self.addCleanup(service.close)
        return service

    def finish(self, service):
        service._main_future.result(timeout=30)
        return service.status()["main"]["status"]

    def test_update_publishes_and_failed_validation_keeps_live_catalog(self):
        self.responses["lxns_songs.json"] = document([song(1), song(2, remaster=True), song(3)])
        failing = self.service(validate_fn=lambda stage: 1 / 0)
        revision = failing.status()["revision"]
        failing.start()
        self.assertEqual(self.finish(failing), "failed")
        self.assertEqual(failing.status()["revision"], revision)
        self.assertNotIn("3:3", self.store.catalog)
        service = self.service()
        service.start()
        self.assertEqual(self.finish(service), "succeeded")
        self.assertNotEqual(service.status()["revision"], revision)
        self.assertIn("3:3", self.store.catalog)

    def test_record_decisions_are_validated_saved_and_revoked(self):
        service = self.service()
        self.assertNotIn("1:3", self.store.catalog)
        review = service.record_review()
        [record] = review["records"]
        base = {
            "base_catalog_revision": review["revision"],
            "base_decision_revision": review["decision_revision"],
        }
        choices = [
            {"field_key": f["field_key"], "option_id": f["options"][0]["option_id"]}
            for f in record["fields"]
        ]
        item = {"id": 1, "difficulty": 3}
        for bad in (
            base | {"records": [item | {"choices": choices[:1]}]},
            base | {"records": [item | {"choices": choices}, item | {"choices": choices}]},
            base | {"records": [{"id": 1, "difficulty": 4, "choices": choices}]},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                service.decide_records(bad)
        with self.assertRaises(StaleDecisionError):
            service.decide_records(base | {"base_decision_revision": 9, "records": []})
        self.assertEqual(service.decisions.snapshot()["decisions"], [])

        service.decide_records(base | {"records": [item | {"choices": choices}]})
        self.assertEqual(self.finish(service), "succeeded")
        self.assertIn("1:3", self.store.catalog)
        self.assertEqual(service.record_review()["records"], [])
        with self.assertRaises(StaleDecisionError):
            service.decide_records(base | {"records": [item | {"choices": choices}]})

        current = service.record_review()
        revoke = item | {
            "base_catalog_revision": current["revision"],
            "base_decision_revision": current["decision_revision"],
        }
        with self.assertRaises(ValueError):
            service.revoke_record(revoke | {"field_keys": ["artist", "not_a_field"]})
        service.revoke_record(revoke | {"field_keys": ["artist"]})
        self.assertEqual(self.finish(service), "succeeded")
        [pending] = service.record_review()["records"]
        self.assertEqual([f["field_key"] for f in pending["fields"]], ["artist"])
        self.assertIn("1:3", self.store.catalog)  # Published values stay while re-reviewed.


class DecisionStoreTests(unittest.TestCase):
    def test_decisions_are_durable_atomic_and_reject_stale_revisions(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "decisions.sqlite3"
            db = CatalogDecisions(path)
            value = {"object_key": "1", "kind": "field:title", "action": "select", "value": "A"}
            self.assertEqual(db.save_many([value], 0), 1)
            with self.assertRaises(StaleDecisionError):
                db.save_many([value | {"value": "B"}], 0)
            invalid = [value | {"value": "C"}, {"object_key": "1", "kind": "x", "action": "revoke"}]
            with self.assertRaisesRegex(ValueError, "没有可撤销"):
                db.save_many(invalid, 1)
            restarted = CatalogDecisions(path)
            self.assertEqual(restarted.snapshot()["decisions"][0]["value"], "A")
            self.assertEqual(restarted.save_many([value | {"action": "revoke"}], 1), 2)
            self.assertEqual(restarted.snapshot(), {"revision": 2, "decisions": []})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
