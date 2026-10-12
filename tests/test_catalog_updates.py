import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.catalog_decisions import StaleDecisionError
from backend.catalog_updates import (
    BusyError,
    CatalogUpdates,
    conflict_fingerprint,
    digest,
    public_warnings,
    read,
    write,
)


def fixture(directory, generation, verified=False):
    directory.mkdir(parents=True, exist_ok=True)
    chart = {
        "chart_key": "1:dx:4",
        "song_id": 1,
        "difficulty_index": 4,
        "constant": generation,
        "jp_release_version": "FESTiVAL" if verified else None,
        "jp_release_provider": "remywiki" if verified else None,
    }
    write(directory / "catalog.json", {"generation": generation, "charts": [chart]})
    temp = directory / "catalog.sqlite.tmp"
    temp.unlink(missing_ok=True)
    conn = sqlite3.connect(temp)
    conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value_json TEXT)")
    conn.execute(
        "INSERT INTO metadata VALUES ('snapshot_at', ?)",
        (json.dumps(f"2026-10-{generation + 1:02d}T00:00:00Z"),),
    )
    conn.commit()
    conn.close()
    temp.replace(directory / "catalog.sqlite")


class FixtureStore:
    def __init__(self, directory):
        self.catalog_root = directory.resolve()
        self.personal = {"1:dx:4": {"ap": True, "venue": "机厅", "note": "不应变化"}}
        self.install_catalog(self.load_catalog(self.catalog_directory()))

    def catalog_directory(self):
        current = self.catalog_root / "current"
        return current.resolve() if current.exists() else self.catalog_root

    def load_catalog(self, directory):
        data = read(directory / "catalog.json")
        return {r["chart_key"]: r for r in data["charts"]}, {}

    def install_catalog(self, loaded):
        self.catalog, self.waterfish_map = loaded


def no_remy(*args, **kwargs):
    return {
        "pending": [],
        "target_count": 1,
        "resolved_count": 0,
        "conflicts": [],
        "fetch_count": 0,
        "additions": [],
        "proposed_overrides": {"overrides": []},
    }


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        fixture(self.root, 0)
        self.store = FixtureStore(self.root)
        self.generation = 0
        self.releases = []
        self.services = []
        self.addCleanup(self.shutdown)

    def shutdown(self):
        for event in self.releases:
            event.set()
        for service in self.services:
            service.close()
            service._main_pool.shutdown(wait=True)
            service._remy_pool.shutdown(wait=True)

    def prepare(self, stage, previous, progress):
        self.generation += 1
        progress("sources", {"source": "lxns"})
        fixture(stage, self.generation)
        return {
            "diff": {"counts": {"added_songs": 1, "added_charts": 4, "constant_changes": 3}},
            "warnings": [{"kind": "optional_source_failed", "message": "水鱼暂不可用，复用缓存"}],
        }

    def service(self, **kwargs):
        options = {
            "prepare_fn": self.prepare,
            "build_fn": lambda p: {"issues": [{"kind": "external_constant_difference"}]},
            "validate_fn": lambda p: {"status": "ok"},
            "remy_fn": no_remy,
        }
        service = CatalogUpdates(self.store, **(options | kwargs))
        self.services.append(service)
        return service

    def finish(self, service, remy=True):
        service._main_future.result(timeout=5)
        if remy and service._remy_future:
            service._remy_future.result(timeout=5)

    def test_success_preserves_baseline_personal_and_presents_diff(self):
        before = digest(self.root / "catalog.sqlite")
        service = self.service()
        self.assertEqual(service.start()["main"]["status"], "running")
        self.finish(service)
        state = service.status()
        self.assertEqual(state["main"]["status"], "succeeded")
        self.assertEqual(state["main"]["diff"]["charts_added"], 4)
        self.assertTrue(all(isinstance(w, str) for w in state["main"]["warnings"]))
        self.assertEqual(state["remy"]["status"], "complete")
        self.assertTrue(state["can_rollback"])
        self.assertEqual(digest(self.root / "catalog.sqlite"), before)
        self.assertEqual(self.store.catalog["1:dx:4"]["constant"], 1)
        self.assertEqual(self.store.personal["1:dx:4"]["venue"], "机厅")

    def test_validation_failure_keeps_pointer_and_live_data(self):
        def fail(stage):
            raise ValueError("bad catalog")

        service = self.service(validate_fn=fail)
        before = service.status()["revision"]
        service.start()
        self.finish(service)
        self.assertEqual(service.status()["main"]["status"], "failed")
        self.assertEqual(service.status()["revision"], before)
        self.assertFalse((self.root / "current").exists())
        self.assertEqual(self.store.catalog["1:dx:4"]["constant"], 0)

    def test_duplicate_update_rejected_and_rollback_preserves_personal(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def prepare(stage, previous, progress):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.prepare(stage, previous, progress)

        service = self.service(prepare_fn=prepare)
        service.start()
        self.assertTrue(entered.wait(3))
        with self.assertRaises(BusyError):
            service.start()
        release.set()
        self.finish(service)
        service.rollback()
        self.finish(service, remy=False)
        self.assertEqual(self.store.catalog["1:dx:4"]["constant"], 0)
        self.assertEqual(service.status()["main"]["status"], "succeeded")
        self.assertTrue(self.store.personal["1:dx:4"]["ap"])

    def test_remy_network_is_independent_and_stale_job_cannot_overwrite(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []

        def remy(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(5))
            return no_remy()

        service = self.service(remy_fn=remy)
        service.start()
        self.finish(service, remy=False)
        self.assertTrue(entered.wait(3))
        self.assertEqual(service.status()["main"]["status"], "succeeded")
        self.assertEqual(service.status()["remy"]["status"], "running")
        service.start()
        self.finish(service, remy=False)
        self.assertEqual(self.store.catalog["1:dx:4"]["constant"], 2)
        release.set()
        service._remy_future.result(timeout=5)
        self.assertEqual(self.store.catalog["1:dx:4"]["constant"], 2)

    def test_remy_build_does_not_hold_main_lease(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []

        def remy(*args, **kwargs):
            calls.append(1)
            result = no_remy()
            if len(calls) == 1:
                result.update(additions=[{"chart_key": "1:dx:4"}], resolved_count=1)
            return result

        def builder(stage):
            if stage.name.startswith("remy-"):
                entered.set()
                self.assertTrue(release.wait(5))
                fixture(stage, 9, verified=True)
            return {"issues": []}

        service = self.service(remy_fn=remy, build_fn=builder)
        service.start()
        self.finish(service, remy=False)
        self.assertTrue(entered.wait(3))
        service.start()  # Must not be rejected by Remy's local build.
        self.finish(service, remy=False)
        current = service.status()["revision"]
        release.set()
        service._remy_future.result(timeout=5)
        self.assertEqual(service.status()["revision"], current)
        self.assertEqual(self.store.catalog["1:dx:4"]["constant"], 2)

    def test_remy_can_enrich_without_consuming_previous_cn_snapshot(self):
        def remy(*a, **kw):
            return no_remy() | {"additions": [{"chart_key": "1:dx:4"}], "resolved_count": 1}

        def builder(stage):
            if stage.name.startswith("remy-"):
                fixture(stage, 1, verified=True)
            return {"issues": []}

        service = self.service(remy_fn=remy, build_fn=builder)
        service.start()
        self.finish(service)
        self.assertTrue(service.status()["revision"].startswith("remy-"))
        self.assertEqual(self.store.catalog["1:dx:4"]["jp_release_version"], "FESTiVAL")
        service.rollback()
        self.finish(service, remy=False)
        self.assertEqual(self.store.catalog["1:dx:4"]["constant"], 0)

    def test_remy_failure_does_not_fail_catalog(self):
        def fail(*a, **kw):
            raise OSError("Remy unavailable")

        service = self.service(remy_fn=fail)
        service.start()
        self.finish(service)
        self.assertEqual(service.status()["main"]["status"], "succeeded")
        self.assertEqual(service.status()["remy"]["status"], "failed")
        self.assertEqual(self.store.catalog["1:dx:4"]["constant"], 1)

    def test_install_failure_restores_previous_pointer(self):
        service = self.service()
        install = self.store.install_catalog
        calls = []

        def fail_once(value):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("install failed")
            install(value)

        with patch.object(self.store, "install_catalog", side_effect=fail_once):
            service.start()
            self.finish(service)
        self.assertFalse((self.root / "current").exists())
        self.assertEqual(self.store.catalog["1:dx:4"]["constant"], 0)
        self.assertEqual(service.status()["main"]["status"], "failed")

    def test_post_publish_status_write_failure_still_reports_published(self):
        service = self.service()
        save = service._save

        def fail_after_publish():
            if (self.root / "current").exists():
                raise OSError("disk full")
            save()

        with patch.object(service, "_save", side_effect=fail_after_publish):
            service.start()
            self.finish(service, remy=False)
            self.assertEqual(service.status()["main"]["status"], "succeeded")
            self.assertTrue((self.root / "current").exists())
            self.assertIn("已发布", service.status()["main"]["warnings"][-1])
        recovered = self.service()
        self.assertEqual(recovered.status()["revision"], service.status()["revision"])
        self.assertEqual(recovered.status()["main"]["status"], "succeeded")

    def test_restart_interrupted_update_can_be_retried(self):
        service = self.service()
        service._state["main"].update(status="running", job_id="interrupted")
        service._save()
        restarted = self.service()
        self.assertEqual(restarted.status()["main"]["status"], "failed")
        restarted.start()
        self.finish(restarted)
        self.assertEqual(restarted.status()["main"]["status"], "succeeded")

    def chart_fixture(self, service):
        fields = [
            {
                "field_key": "artist",
                "label": "作者",
                "required": True,
                "options": [
                    {"option_id": "artist-a", "source": "arcade_songs", "value": "Selected Artist"}
                ],
            },
            {
                "field_key": "constant",
                "label": "定数",
                "required": True,
                "options": [{"option_id": "constant-b", "source": "divingfish", "value": 13.9}],
            },
        ]

        def builder(stage):
            document = read(stage / "catalog.json")
            active = read(stage / "catalog_adjudications.json")
            values = {
                d["field_key"]: d
                for d in active["decisions"]
                if d.get("kind") == "chart_field"
                and d.get("id") == 10001
                and d.get("difficulty") == 4
            }
            missing = [field for field in fields if field["field_key"] not in values]
            chart = {
                "chart_key": "10001:4",
                "id": 10001,
                "difficulty": 4,
                "type": "dx",
                "title": "Test",
                "artist": "Source Artist",
                "constant": document.get("generation", 0),
                "jp_release_version": "Splash",
                "jp_release_provider": "remywiki",
            }
            for key, decision in values.items():
                chart[key] = decision["value"]
            counts = {"total": 1, "pending": int(bool(missing)), "admitted": int(not missing)}
            record = {
                "id": 10001,
                "difficulty": 4,
                "chart_key": "10001:4",
                "title": "Test",
                "type": "dx",
                "difficulty_name": "Re:MASTER",
                "status": "pending" if missing else "resolved",
                "fields": missing,
            }
            document.update(
                schema_version=2,
                charts=[] if missing else [chart],
                decision_revision=active["revision"],
            )
            write(stage / "catalog.json", document)
            write(
                stage / "catalog_record_review.json",
                {"schema_version": 2, "counts": counts, "records": [record]},
            )
            return {"schema_version": 2, "counts": counts, "issues": []}

        def validate_choices(review, ident, difficulty, choices):
            row = next(
                (
                    r
                    for r in review["records"]
                    if r["id"] == ident and r["difficulty"] == difficulty
                ),
                None,
            )
            if not row or row["status"] != "pending":
                raise ValueError("not pending")
            required = {f["field_key"]: f for f in row["fields"] if f["required"]}
            selected = [choice["field_key"] for choice in choices]
            if set(selected) != set(required) or len(selected) != len(set(selected)):
                raise ValueError("select every conflicting field")
            values = []
            for choice in choices:
                option = next(
                    (
                        o
                        for o in required[choice["field_key"]]["options"]
                        if o["option_id"] == choice["option_id"]
                    ),
                    None,
                )
                if not option:
                    raise ValueError("invalid source option")
                values.append(
                    {
                        "object_key": f"chart:{ident}:{difficulty}:{choice['field_key']}",
                        "kind": "chart_field",
                        "id": ident,
                        "difficulty": difficulty,
                        "field_key": choice["field_key"],
                        "action": "select",
                        "value": option["value"],
                        "source": option["source"],
                    }
                )
            return values

        module = SimpleNamespace(
            load_record_review=lambda directory, **kw: read(
                directory / "catalog_record_review.json"
            ),
            validate_chart_decisions=validate_choices,
        )
        self.enterContext(patch.dict(sys.modules, {"scripts.catalog_chart_compiler": module}))
        service._build = builder
        write(self.root / "catalog_adjudications.json", service.decisions.snapshot())
        builder(self.root)
        self.store.install_catalog(self.store.load_catalog(self.root))
        return builder

    def chart_request(self, service, **changes):
        return {
            "base_catalog_revision": service.status()["revision"],
            "base_decision_revision": service.status()["decision_revision"],
            "id": 10001,
            "difficulty": 4,
            "choices": [
                {"field_key": "artist", "option_id": "artist-a"},
                {"field_key": "constant", "option_id": "constant-b"},
            ],
            **changes,
        }

    def revoke_request(self, service, **changes):
        return {
            key: value
            for key, value in self.chart_request(service, **changes).items()
            if key != "choices"
        }

    def test_chart_decisions_survive_source_update_rollback_restart_until_explicit_revoke(self):
        service = self.service()
        self.chart_fixture(service)
        self.assertEqual(self.store.catalog, {})
        service.decide_record(self.chart_request(service))
        self.finish(service)
        self.assertEqual(self.store.catalog["10001:4"]["artist"], "Selected Artist")
        self.assertEqual(self.store.catalog["10001:4"]["constant"], 13.9)
        self.assertEqual(service.record_review()["records"], [])
        service.start()
        self.finish(service)
        self.assertEqual(self.store.catalog["10001:4"]["constant"], 13.9)
        service.rollback()
        self.finish(service, remy=False)
        self.assertEqual(self.store.catalog["10001:4"]["constant"], 13.9)
        restarted = self.service(build_fn=service._build)
        self.assertEqual(len(restarted.manual_records()["records"][0]["fields"]), 2)
        restarted.revoke_record(self.revoke_request(restarted, field_keys=["artist"]))
        self.finish(restarted)
        self.assertEqual(self.store.catalog, {})
        pending = restarted.record_review()["records"][0]
        self.assertEqual([f["field_key"] for f in pending["fields"]], ["artist"])
        self.assertEqual(restarted.decisions.snapshot()["decisions"][0]["field_key"], "constant")
        self.assertTrue(self.store.personal["1:dx:4"]["ap"])

    def test_failed_chart_build_stays_out_of_formal_catalog_and_reapply_recovers(self):
        service = self.service()
        self.chart_fixture(service)
        before = service.status()["revision"]
        service._validate = lambda stage: (_ for _ in ()).throw(ValueError("validation failed"))
        service.decide_record(self.chart_request(service))
        self.finish(service)
        self.assertEqual(service.status()["main"]["status"], "failed")
        self.assertEqual(service.status()["revision"], before)
        self.assertTrue(service.status()["decisions_pending"])
        self.assertEqual(self.store.catalog, {})
        self.assertEqual(len(service.decisions.snapshot()["decisions"]), 2)
        service._validate = lambda stage: None
        service.reapply_decisions()
        self.finish(service)
        self.assertFalse(service.status()["decisions_pending"])
        self.assertEqual(self.store.catalog["10001:4"]["constant"], 13.9)

    def test_old_remy_result_cannot_overwrite_chart_decisions(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def remy(*a, **kw):
            entered.set()
            self.assertTrue(release.wait(5))
            return no_remy() | {"additions": [{"chart_key": "1:dx:4"}], "resolved_count": 1}

        service = self.service(remy_fn=remy)
        self.chart_fixture(service)
        service.start()
        self.finish(service, remy=False)
        self.assertTrue(entered.wait(3))
        old_future = service._remy_future
        service.decide_record(self.chart_request(service))
        service._main_future.result(timeout=5)
        revision = service.status()["revision"]
        release.set()
        old_future.result(timeout=5)
        self.assertEqual(service.status()["revision"], revision)
        self.assertEqual(self.store.catalog["10001:4"]["artist"], "Selected Artist")

    def test_recheck_is_explicit_and_conflicts_publish_without_additions(self):
        called = []
        conflict = {"chart_key": "1:dx:4", "reason": "different_release", "candidates": []}

        def remy(*a, **kw):
            called.append(kw.get("recheck", False))
            return no_remy() | {"conflicts": [conflict] if kw.get("recheck") else []}

        service = self.service(remy_fn=remy)
        service.start()
        self.finish(service)
        previous = service.status()["revision"]
        service.retry_remy(recheck=True)
        service._remy_future.result(timeout=5)
        self.assertEqual(called, [False, True])
        self.assertNotEqual(service.status()["revision"], previous)
        path = self.store.catalog_directory() / "remy_conflicts.json"
        self.assertEqual(read(path)["conflicts"], [conflict])

    def test_conflict_identity_ignores_local_path_and_fetch_time(self):
        value = {
            "chart_key": "1:dx:4",
            "reason": "conflict",
            "candidates": [
                {
                    "jp_release_version": "Splash",
                    "evidence_sha256": "abc",
                    "evidence_file": "/cache/page.html",
                    "retrieved_at": "yesterday",
                }
            ],
        }
        saved = json.loads(json.dumps(value))
        saved["candidates"][0].update(evidence_file="remy-evidence/abc.html", retrieved_at="today")
        self.assertEqual(conflict_fingerprint(value), conflict_fingerprint(saved))
        saved["candidates"][0]["jp_release_version"] = "DX"
        self.assertNotEqual(conflict_fingerprint(value), conflict_fingerprint(saved))

    def test_blocked_recheck_does_not_claim_finished(self):
        def remy(*a, **kw):
            return no_remy() | {"recheck_complete": False, "fetch_errors": [{"kind": "http_403"}]}

        service = self.service(remy_fn=remy)
        service.retry_remy(recheck=True)
        service._remy_future.result(timeout=5)
        self.assertEqual(service.status()["remy"]["status"], "pending")
        self.assertIn("重核未完成", service.status()["remy"]["message"])

    def test_whole_chart_gate_rejects_partial_invalid_cross_chart_and_duplicate_requests(self):
        service = self.service()
        self.chart_fixture(service)
        good = self.chart_request(service)
        invalid = [
            good | {"choices": good["choices"][:1]},
            good | {"choices": []},
            good | {"id": 1},
            good | {"difficulty": 3},
            good | {"choices": [good["choices"][0], good["choices"][0]]},
            good
            | {"choices": [good["choices"][0], {"field_key": "constant", "option_id": "gone"}]},
        ]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                service.decide_record(request)
            self.assertEqual(service.decisions.snapshot()["decisions"], [])
            self.assertEqual(service.decisions.history(), [])
            self.assertIsNone(service._main_future)

    def test_complete_chart_choices_save_one_batch_rebuild_once_and_reject_stale_repeat(self):
        service = self.service()
        builder = self.chart_fixture(service)
        calls = []

        def counting_builder(stage):
            calls.append(stage)
            return builder(stage)

        service._build = counting_builder
        request = self.chart_request(service)
        service.decide_record(request)
        self.finish(service)
        self.assertEqual(len(calls), 1)
        self.assertEqual(service.decisions.snapshot()["revision"], 2)
        self.assertEqual({event["batch_id"] for event in service.decisions.history()}, {1})
        with self.assertRaises(StaleDecisionError):
            service.decide_record(request)
        self.assertEqual(len(calls), 1)
        service.revoke_record(self.revoke_request(service))
        self.finish(service)
        self.assertEqual(len(calls), 2)
        self.assertEqual(service.decisions.snapshot()["decisions"], [])
        self.assertEqual(self.store.catalog, {})

    def test_unknown_revoke_field_rolls_back_whole_request(self):
        service = self.service()
        self.chart_fixture(service)
        service.decide_record(self.chart_request(service))
        self.finish(service)
        before = service.decisions.snapshot()
        with self.assertRaises(ValueError):
            service.revoke_record(
                self.revoke_request(service, field_keys=["artist", "not_a_field"])
            )
        self.assertEqual(service.decisions.snapshot(), before)
        self.assertEqual(self.store.catalog["10001:4"]["artist"], "Selected Artist")

    def batch_fixture(self, service):
        specs = [
            (383, 1, [("title", "Link", "Link(CoF)")]),
            (383, 2, [("title", "Link", "Link(CoF)"), ("constant", 9.7, 9.8)]),
        ]

        def builder(stage):
            document = read(stage / "catalog.json")
            active = read(stage / "catalog_adjudications.json")
            decisions = {(d["id"], d["difficulty"], d["field_key"]): d for d in active["decisions"]}
            records, charts = [], []
            for ident, difficulty, conflicts in specs:
                fields = [
                    {
                        "field_key": field,
                        "label": field,
                        "required": True,
                        "options": [
                            {
                                "option_id": f"{ident}:{difficulty}:{field}:lxns",
                                "source": "lxns",
                                "value": first,
                            },
                            {
                                "option_id": f"{ident}:{difficulty}:{field}:divingfish",
                                "source": "divingfish",
                                "value": second,
                            },
                        ],
                    }
                    for field, first, second in conflicts
                    if (ident, difficulty, field) not in decisions
                ]
                records.append(
                    {
                        "id": ident,
                        "difficulty": difficulty,
                        "chart_key": f"{ident}:{difficulty}",
                        "title": "Link",
                        "type": "standard",
                        "difficulty_name": ["Basic", "Advanced", "Expert"][difficulty],
                        "status": "pending" if fields else "resolved",
                        "fields": fields,
                    }
                )
                if not fields:
                    chart = {
                        "id": ident,
                        "difficulty": difficulty,
                        "chart_key": f"{ident}:{difficulty}",
                    }
                    chart.update(
                        {
                            field: decisions[(ident, difficulty, field)]["value"]
                            for field, _, _ in conflicts
                        }
                    )
                    charts.append(chart)
            counts = {
                "total": len(records),
                "pending": len(records) - len(charts),
                "admitted": len(charts),
            }
            document.update(schema_version=2, charts=charts, decision_revision=active["revision"])
            write(stage / "catalog.json", document)
            write(
                stage / "catalog_record_review.json",
                {"schema_version": 2, "counts": counts, "records": records},
            )
            return {"schema_version": 2, "counts": counts, "issues": []}

        service._build = builder
        write(self.root / "catalog_adjudications.json", service.decisions.snapshot())
        builder(self.root)
        self.store.install_catalog(self.store.load_catalog(self.root))
        report = service.record_review()
        return {
            "base_catalog_revision": report["revision"],
            "base_decision_revision": report["decision_revision"],
            "records": [
                {
                    "id": record["id"],
                    "difficulty": record["difficulty"],
                    "note": f"difficulty {record['difficulty']}",
                    "choices": [
                        {
                            "field_key": field["field_key"],
                            "option_id": field["options"][0]["option_id"],
                        }
                        for field in record["fields"]
                    ],
                }
                for record in report["records"]
            ],
        }

    def test_multi_chart_batch_uses_one_review_transaction_and_rebuild(self):
        service = self.service()
        request = self.batch_fixture(service)
        personal = json.loads(json.dumps(self.store.personal))
        with (
            patch.object(service, "record_review", wraps=service.record_review) as reviews,
            patch.object(
                service.decisions, "save_many", wraps=service.decisions.save_many
            ) as saves,
            patch.object(service, "_build", wraps=service._build) as builds,
        ):
            service.decide_records(request)
            self.finish(service)
            self.assertEqual(reviews.call_count, 1)
            self.assertEqual(saves.call_count, 1)
            self.assertEqual(builds.call_count, 1)
            with self.assertRaises(StaleDecisionError):
                service.decide_records(request)
            self.assertEqual(saves.call_count, 1)
            self.assertEqual(builds.call_count, 1)
        document = service.decisions.snapshot()
        self.assertEqual(document["revision"], 3)
        self.assertEqual(len(document["decisions"]), 3)
        self.assertEqual({event["batch_id"] for event in service.decisions.history()}, {1})
        self.assertEqual(
            {value["note"] for value in document["decisions"]}, {"difficulty 1", "difficulty 2"}
        )
        self.assertEqual(set(self.store.catalog), {"383:1", "383:2"})
        self.assertEqual(self.store.catalog["383:2"]["constant"], 9.7)
        self.assertEqual(service.record_review()["records"], [])
        self.assertEqual(service.status()["main"]["status"], "succeeded")
        self.assertFalse(service.status()["decisions_pending"])
        self.assertEqual(self.store.personal, personal)

    def test_invalid_later_chart_leaves_entire_batch_unwritten(self):
        service = self.service()
        request = self.batch_fixture(service)
        first, second = request["records"]
        invalid_records = [
            [first, second | {"choices": second["choices"][:1]}],
            [
                first,
                second
                | {
                    "choices": [
                        second["choices"][0],
                        {"field_key": "constant", "option_id": "gone"},
                    ]
                },
            ],
            [first, second | {"choices": second["choices"] + [second["choices"][0]]}],
            [first, first],
            [first, second | {"id": 999}],
            [first, second | {"difficulty": True}],
            [],
        ]
        before = service.decisions.snapshot()
        for records in invalid_records:
            with self.subTest(records=records), self.assertRaises(ValueError):
                service.decide_records(request | {"records": records})
            self.assertEqual(service.decisions.snapshot(), before)
            self.assertEqual(service.decisions.history(), [])
            self.assertIsNone(service._main_future)
            self.assertEqual(self.store.catalog, {})

    def test_multi_chart_batch_rejects_stale_bases_before_any_write(self):
        service = self.service()
        request = self.batch_fixture(service)
        for change in ({"base_catalog_revision": "old"}, {"base_decision_revision": 99}):
            with self.subTest(change=change), self.assertRaises(StaleDecisionError):
                service.decide_records(request | change)
            self.assertEqual(service.decisions.snapshot()["decisions"], [])
            self.assertEqual(service.decisions.history(), [])
            self.assertIsNone(service._main_future)

    def test_pending_api_and_manual_list_are_separate_and_remy_counts_include_pending_charts(self):
        service = self.service()
        self.chart_fixture(service)
        report = service.record_review()
        self.assertEqual(report["counts"], {"total": 1, "admitted": 0, "pending": 1})
        self.assertEqual(len(report["records"]), 1)
        self.assertEqual(service.manual_records()["records"], [])
        # Author/constant conflicts quarantine the chart without making a verified JP release unknown.
        self.assertEqual(service._remy_summary()["verified_count"], 1)
        service.decide_record(self.chart_request(service))
        self.finish(service)
        self.assertEqual(service.record_review()["records"], [])
        self.assertEqual(service.record_review()["counts"]["admitted"], 1)
        manual = service.manual_records()["records"][0]
        self.assertEqual(
            (manual["id"], manual["difficulty"], manual["chart_key"]), (10001, 4, "10001:4")
        )
        self.assertEqual({f["field_key"] for f in manual["fields"]}, {"artist", "constant"})

    def test_decision_migration_is_explicit_auditable_and_idempotent(self):
        service = self.service()
        service.decisions.save(
            {
                "object_key": "field:1:song.artist",
                "kind": "field",
                "action": "select",
                "value": "Selected Artist",
            },
            0,
        )
        original = service.decisions.snapshot()

        def migrate(document, directory):
            self.assertEqual(document, original)
            return {
                "schema_version": 2,
                "revision": document["revision"],
                "migration": {"converted_count": 1},
                "decisions": [
                    {
                        "object_key": "chart:10001:4:artist",
                        "kind": "chart_field",
                        "id": 10001,
                        "difficulty": 4,
                        "field_key": "artist",
                        "action": "select",
                        "value": "Selected Artist",
                    }
                ],
            }

        with patch.dict(
            sys.modules,
            {"scripts.catalog_chart_compiler": SimpleNamespace(migrate_decisions=migrate)},
        ):
            migrated = service.migrate_legacy_decisions()
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["revision"], 2)
            self.assertEqual(service.migrate_legacy_decisions(), migrated)
        self.assertTrue(service.status()["decisions_pending"])
        self.assertEqual(len(service.decisions.history()), 2)
        self.assertEqual(
            service.decisions.history()[0]["previous"]["decisions"], original["decisions"]
        )
        self.assertIsNone(service._main_future)

    def test_public_warning_counts_only_unadmitted_charts_not_resolved_raw_differences(self):
        built = {
            "schema_version": 2,
            "counts": {"pending": 0, "admitted": 1, "total": 1},
            "issues": [{"kind": "external_constant_difference"}, {"kind": "jp_version_conflict"}],
        }
        self.assertEqual(public_warnings({}, built), [])
        built["counts"]["pending"] = 1
        self.assertEqual(public_warnings({}, built), ["1 张谱面待裁决，尚未入库"])

    def test_remy_summary_includes_unknown_versions_and_quarantined_white_charts(self):
        service = self.service()
        write(
            self.root / "catalog_record_review.json",
            {
                "schema_version": 2,
                "records": [],
                "remy_counts": {"total": 3, "verified": 1, "pending": 2},
            },
        )
        summary = service._remy_summary()
        self.assertEqual((summary["verified_count"], summary["pending_count"]), (1, 2))
        self.assertEqual(summary["status"], "pending")


if __name__ == "__main__":
    unittest.main()
