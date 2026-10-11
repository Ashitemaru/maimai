import tempfile
import unittest
from pathlib import Path

from backend.catalog_decisions import CatalogDecisions, StaleDecisionError


class DecisionsTests(unittest.TestCase):
    def test_restart_revoke_and_history_are_durable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "decisions.sqlite3"
            db = CatalogDecisions(path)
            value = {
                "object_key": "1:dx:ordinary",
                "kind": "version",
                "action": "set",
                "version": "DX",
                "object_fingerprint": "identity",
            }
            self.assertEqual(db.save(value, 0), 1)
            with self.assertRaises(StaleDecisionError):
                db.save(value | {"version": "ORANGE"}, 0)
            self.assertEqual(db.snapshot()["decisions"][0]["version"], "DX")
            restarted = CatalogDecisions(path)
            self.assertEqual(restarted.snapshot()["revision"], 1)
            self.assertEqual(restarted.save(value | {"action": "revoke"}, 1), 2)
            self.assertEqual(restarted.snapshot(), {"revision": 2, "decisions": []})
            history = restarted.history(value["object_key"])
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["previous"]["version"], "DX")
            self.assertEqual(history[0]["decision"]["action"], "revoke")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_identity_and_version_decisions_have_separate_lifetimes(self):
        with tempfile.TemporaryDirectory() as temp:
            db = CatalogDecisions(Path(temp) / "decisions.sqlite3")
            key = {"object_key": "1:dx:ordinary", "action": "select", "candidate_id": "a"}
            db.save(key | {"kind": "identity"}, 0)
            db.save(key | {"kind": "version"}, 1)
            db.save(key | {"kind": "version", "action": "revoke"}, 2)
            self.assertEqual([d["kind"] for d in db.snapshot()["decisions"]], ["identity"])

    def test_many_choices_are_one_transaction_with_linked_history(self):
        with tempfile.TemporaryDirectory() as temp:
            db = CatalogDecisions(Path(temp) / "decisions.sqlite3")
            choices = [
                {"object_key": "1", "kind": "field:artist", "action": "select", "value": "Artist"},
                {
                    "object_key": "1:dx:3",
                    "kind": "field:constant",
                    "action": "select",
                    "value": 13.9,
                },
            ]
            self.assertEqual(db.save_many(choices, 0), 2)
            self.assertEqual(len(db.snapshot()["decisions"]), 2)
            history = db.history()
            self.assertEqual([event["revision"] for event in history], [2, 1])
            self.assertEqual({event["batch_id"] for event in history}, {1})
            self.assertEqual(
                {event["decision"]["updated_at"] for event in history}, {history[0]["created_at"]}
            )
            with self.assertRaises(StaleDecisionError):
                db.save_many(choices, 0)
            self.assertEqual(db.snapshot()["revision"], 2)

    def test_invalid_later_revoke_never_partially_saves_earlier_choice(self):
        with tempfile.TemporaryDirectory() as temp:
            db = CatalogDecisions(Path(temp) / "decisions.sqlite3")
            choices = [
                {
                    "object_key": "1",
                    "kind": "field:title",
                    "action": "select",
                    "value": "new title",
                },
                {"object_key": "1", "kind": "field:artist", "action": "revoke"},
            ]
            with self.assertRaisesRegex(ValueError, "没有可撤销"):
                db.save_many(choices, 0)
            self.assertEqual(db.snapshot(), {"revision": 0, "decisions": []})
            self.assertEqual(db.history(), [])
            with self.assertRaisesRegex(ValueError, "重复裁决"):
                db.save_many([choices[0], choices[0]], 0)
            self.assertEqual(db.history(), [])

    def test_explicit_schema_migration_preserves_old_choices_in_audit_and_new_values(self):
        with tempfile.TemporaryDirectory() as temp:
            db = CatalogDecisions(Path(temp) / "decisions.sqlite3")
            legacy = {
                "object_key": "field:1:song.artist",
                "kind": "field",
                "action": "select",
                "value": "Chosen Artist",
            }
            db.save(legacy, 0)
            original = db.snapshot()
            migrated = {
                "schema_version": 2,
                "revision": 1,
                "migration": {"from_schema": 1},
                "decisions": [
                    {
                        "object_key": "chart:10001:3:artist",
                        "kind": "chart_field",
                        "id": 10001,
                        "difficulty": 3,
                        "field_key": "artist",
                        "action": "select",
                        "value": "Chosen Artist",
                    }
                ],
            }
            self.assertEqual(db.migrate_document(migrated, 1), 2)
            snapshot = db.snapshot()
            self.assertEqual(snapshot["schema_version"], 2)
            self.assertEqual(snapshot["revision"], 2)
            self.assertEqual(snapshot["decisions"][0]["value"], "Chosen Artist")
            self.assertEqual(snapshot["decisions"][0]["migration_revision"], 2)
            history = db.history()
            self.assertEqual(history[0]["decision"]["kind"], "migration")
            self.assertEqual(history[0]["previous"]["decisions"], original["decisions"])
            self.assertEqual(len(history), 2)
            reopened = CatalogDecisions(db.path)
            self.assertEqual(reopened.snapshot(), snapshot)

    def test_migration_validation_or_stale_revision_never_replaces_old_decisions(self):
        with tempfile.TemporaryDirectory() as temp:
            db = CatalogDecisions(Path(temp) / "decisions.sqlite3")
            db.save(
                {"object_key": "1:dx:4", "kind": "version", "action": "set", "version": "DX"}, 0
            )
            original = db.snapshot()
            with self.assertRaises(ValueError):
                db.migrate_document(
                    {"schema_version": 2, "revision": 1, "decisions": original["decisions"]}, 1
                )
            self.assertEqual(db.snapshot(), original)
            with self.assertRaises(StaleDecisionError):
                db.migrate_document({"schema_version": 2, "revision": 0, "decisions": []}, 0)
            self.assertEqual(db.snapshot(), original)
            self.assertEqual(len(db.history()), 1)

    def test_schema_two_bootstrap_restores_marker_and_durable_value(self):
        with tempfile.TemporaryDirectory() as temp:
            document = {
                "schema_version": 2,
                "revision": 9,
                "decisions": [
                    {
                        "object_key": "chart:10001:3:constant",
                        "kind": "chart_field",
                        "id": 10001,
                        "difficulty": 3,
                        "field_key": "constant",
                        "action": "select",
                        "value": 13.9,
                    }
                ],
            }
            db = CatalogDecisions(Path(temp) / "decisions.sqlite3", bootstrap=document)
            self.assertEqual(db.snapshot(), document)


if __name__ == "__main__":
    unittest.main()
