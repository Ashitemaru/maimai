import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.ap_records import RecordConflictError
from backend.store import Store
from backend.test_app import FakeUpdater
from tests import test_chart_store
from tests.support import create_app


class APRecordTests(unittest.TestCase):
    setUp = test_chart_store.ChartStoreTests.setUp
    tearDown = test_chart_store.ChartStoreTests.tearDown
    key = "10001:3"

    def save(self, *, venue="【测试市】测试机厅", request_id="request-1", revision=0):
        return self.store.save_ap_record(
            self.key,
            {
                "venue": venue,
                "request_id": request_id,
                "expected_revision": revision,
            },
        )

    def score(self, fc="ap", **overrides):
        return {
            "song_id": 10001,
            "level_index": 3,
            "type": "DX",
            "fc": fc,
            "achievements": 100.9622,
            "dxScore": 1234,
            "fs": "fsp",
            "rate": "sssp",
            "ra": 299,
            **overrides,
        }

    def sync(self, fc="ap", **overrides):
        return self.store.import_provider({"records": [self.score(fc, **overrides)]})

    def test_pending_persists_without_changing_score_or_completion(self):
        chart = self.save(venue="  【测试市】测试机厅  ")
        self.assertEqual(chart["pending_record"]["venue"], "【测试市】测试机厅")
        self.assertFalse(chart["is_ap"])
        self.assertFalse(chart["is_complete"])
        self.assertEqual(chart["venue"], "")
        self.assertIsNone(chart["provider_achievement"])
        restarted = Store(runtime=self.runtime, catalog_dir=self.catalog)
        self.assertEqual(restarted.chart(self.key)["pending_record"], chart["pending_record"])
        self.assertEqual(restarted.ap_records()["count"], 1)
        self.assertEqual(restarted.venue_list()["items"][0]["name"], "【测试市】测试机厅")

    def test_idempotency_cannot_resurrect_cancelled_or_confirmed_claim(self):
        chart = self.save()
        self.assertEqual(self.save()["record_revision"], chart["record_revision"])
        self.store.cancel_ap_record(self.key, chart["record_revision"])
        self.assertIsNone(self.save()["pending_record"])
        with self.assertRaisesRegex(ValueError, "请求编号"):
            self.save(venue="different")

    def test_edit_and_cancel_check_revision(self):
        chart = self.save()
        with self.assertRaises(RecordConflictError):
            self.save(request_id="stale")
        edited = self.save(venue="新机厅", request_id="edit", revision=chart["record_revision"])
        with self.assertRaises(RecordConflictError):
            self.store.cancel_ap_record(self.key, chart["record_revision"])
        cleared = self.store.cancel_ap_record(self.key, edited["record_revision"])
        self.assertIsNone(cleared["pending_record"])
        self.assertGreater(cleared["record_revision"], edited["record_revision"])

    def test_non_ap_and_missing_scores_remain_pending(self):
        self.save()
        receipt = self.sync("fcp", achievements=101)
        chart = self.store.chart(self.key)
        self.assertEqual((receipt["confirmed"], receipt["pending"]), (0, 1))
        self.assertFalse(chart["is_ap"])
        self.assertFalse(chart["is_complete"])
        self.assertFalse(chart["conflict"])
        self.assertEqual(chart["pending_record"]["reason"], "水鱼暂未显示 AP")
        self.store.import_provider({"records": []})
        self.assertEqual(self.store.chart(self.key)["pending_record"]["reason"], "水鱼暂无这张成绩")

    def test_sync_promotes_venue_and_saves_personal_score_evidence_atomically(self):
        pending = self.save()["pending_record"]
        receipt = self.sync("app", achievements=101)
        chart = self.store.chart(self.key)
        self.assertEqual((receipt["confirmed"], receipt["pending"]), (1, 0))
        self.assertTrue(chart["is_ap"])
        self.assertEqual(chart["status"], "app")
        self.assertEqual(chart["venue"], pending["venue"])
        self.assertFalse(chart["venue_pending"])
        self.assertIsNone(chart["pending_record"])
        self.assertEqual(
            (
                chart["provider_fs"],
                chart["provider_dx_score"],
                chart["provider_rate"],
                chart["provider_ra"],
            ),
            ("fsp", 1234, "sssp", 299),
        )
        with self.store.db() as conn:
            confirmation = conn.execute("SELECT * FROM ap_confirmations").fetchone()
            self.assertEqual(confirmation["sync_id"], receipt["sync_id"])
            self.assertEqual(confirmation["claim_id"], pending["id"])
            self.assertEqual(json.loads(confirmation["evidence_json"])["fc"], "app")
        self.assertIsNone(self.save()["pending_record"])

    def test_provider_ap_counts_and_flags_missing_venue_without_fabricating_one(self):
        self.sync()
        chart = self.store.chart(self.key)
        self.assertTrue(chart["is_ap"])
        self.assertTrue(chart["is_complete"])
        self.assertTrue(chart["venue_pending"])
        self.assertEqual(chart["venue"], "")
        self.assertIsNone(chart["pending_record"])

    def test_low_chart_can_queue_without_venue_and_creates_no_empty_venue(self):
        self.store.catalog[self.key]["constant"] = 10.0
        with TestClient(create_app(self.store, updater=FakeUpdater())) as client:
            response = client.put(
                f"/api/charts/{self.key}/ap-record",
                json={
                    "request_id": "low-claim",
                    "expected_revision": 0,
                    "venue": "",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["chart"]["is_ap"])
            self.assertEqual(response.json()["chart"]["pending_record"]["venue"], "")
        receipt = self.sync()
        self.assertEqual(receipt["confirmed"], 1)
        self.assertEqual(self.store.chart(self.key)["venue"], "")
        self.assertFalse(self.store.chart(self.key)["venue_pending"])
        self.assertEqual(self.store.venue_list()["items"], [])

    def test_orphan_pending_keeps_chart_snapshot_and_can_be_cancelled(self):
        revision = self.save()["record_revision"]
        self.store.catalog.pop(self.key)
        item = self.store.ap_records()["items"][0]
        self.assertEqual(item["title"], "Example")
        self.assertEqual(item["difficulty_name"], "MASTER")
        with TestClient(create_app(self.store, updater=FakeUpdater())) as client:
            path = f"/api/charts/{self.key}/ap-record"
            stale = client.delete(path, params={"expected_revision": revision - 1})
            self.assertEqual(stale.status_code, 409)
            self.assertIsNone(stale.json()["detail"]["chart"])
            deleted = client.delete(path, params={"expected_revision": revision})
            self.assertEqual(deleted.status_code, 200)
            self.assertIsNone(deleted.json()["chart"])
            self.assertEqual(client.get("/api/ap-records").json()["count"], 0)

    def test_durable_sync_receipt_matches_response(self):
        self.save()
        receipt = self.sync()
        self.assertEqual(self.store.meta("last_sync"), receipt)
        with self.store.db() as conn:
            saved = conn.execute("SELECT receipt_json FROM provider_syncs").fetchone()[0]
            self.assertEqual(json.loads(saved), receipt)

    def test_retired_manual_routes_cannot_create_or_remove_ap(self):
        before = self.store.chart(self.key)
        with TestClient(create_app(self.store, updater=FakeUpdater())) as client:
            path = f"/api/charts/{self.key}/manual"
            for response in (
                client.put(path, json={"ap": True, "achievement": 101, "venue": "bypass"}),
                client.delete(path),
            ):
                self.assertEqual(response.status_code, 410)
        self.assertEqual(self.store.chart(self.key), before)

    def test_confirmed_evidence_survives_missing_or_reduced_remote_score(self):
        self.save()
        self.sync("app", achievements=101)
        self.store.import_provider({"records": []})
        chart = self.store.chart(self.key)
        self.assertEqual((chart["status"], chart["venue"]), ("app", "【测试市】测试机厅"))
        self.assertFalse(chart["conflict"])
        self.sync("fc")
        chart = self.store.chart(self.key)
        self.assertTrue(chart["conflict"])
        self.assertEqual(chart["status"], "app")

    def test_venue_correction_retains_confirmed_venue_until_fresh_sync(self):
        self.save()
        self.sync()
        chart = self.store.chart(self.key)
        self.save(venue="新机厅", request_id="correction", revision=chart["record_revision"])
        self.assertEqual(self.store.chart(self.key)["venue"], "【测试市】测试机厅")
        self.sync("fc")
        self.assertEqual(self.store.chart(self.key)["venue"], "【测试市】测试机厅")
        self.sync()
        self.assertEqual(self.store.chart(self.key)["venue"], "新机厅")

    def test_sync_start_snapshot_does_not_promote_new_edited_or_cancelled_claim(self):
        empty = self.store.pending_snapshot()
        chart = self.save()
        self.store.import_provider({"records": [self.score()]}, empty)
        self.assertIsNotNone(self.store.chart(self.key)["pending_record"])
        old = self.store.pending_snapshot()
        edited = self.save(venue="新机厅", request_id="new", revision=chart["record_revision"])
        self.store.import_provider({"records": [self.score()]}, old)
        self.assertEqual(self.store.chart(self.key)["pending_record"]["venue"], "新机厅")
        current = self.store.pending_snapshot()
        self.store.cancel_ap_record(self.key, edited["record_revision"])
        self.store.import_provider({"records": [self.score()]}, current)
        self.assertEqual(self.store.chart(self.key)["venue"], "")
        self.assertIsNone(self.store.chart(self.key)["pending_record"])

    def test_malformed_response_rolls_back_scores_buffer_and_evidence(self):
        self.save()
        self.sync("fc")
        before = self.store.chart(self.key)
        for extra in ({"achievements": float("nan")}, {"fc": "invalid"}, {"dxScore": -1}):
            with self.assertRaises(ValueError):
                self.store.import_provider({"records": [self.score(**extra)]})
            self.assertEqual(self.store.chart(self.key), before)
        with self.store.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM provider_syncs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ap_confirmations").fetchone()[0], 0)

    def test_unmatched_utage_above_101_is_retained_without_polluting_catalog(self):
        utage = self.score(song_id=999999, achievements=150, level_index=5, title="[宴]Example")
        receipt = self.store.import_provider({"records": [self.score(), utage]})
        self.assertEqual((receipt["matched"], receipt["unmatched"]), (1, 1))
        with self.store.db() as conn:
            row = conn.execute("SELECT * FROM provider_unmatched").fetchone()
            self.assertEqual(json.loads(row["raw_json"]), utage)
            history = conn.execute("SELECT records_json FROM provider_syncs").fetchone()[0]
            self.assertEqual(len(json.loads(history)), 2)
        self.assertEqual(len(self.store.all_charts()), 1)
        with self.assertRaises(ValueError):
            self.store.import_provider({"records": [self.score(), self.score()]})

    def test_confirmation_write_failure_rolls_back_whole_sync(self):
        self.save()
        self.sync("fc")
        before = self.store.chart(self.key)
        with self.store.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_confirmation BEFORE INSERT ON ap_confirmations "
                "BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.sync()
        self.assertEqual(self.store.chart(self.key), before)
        with self.store.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ap_evidence").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM provider_syncs").fetchone()[0], 1)

    def test_two_writers_cannot_overwrite_same_revision(self):
        barrier = threading.Barrier(2)

        def save(request_id):
            barrier.wait(timeout=5)
            try:
                self.save(request_id=request_id)
                return "saved"
            except RecordConflictError:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["one", "two"]))
        self.assertCountEqual(results, ["saved", "stale"])

    def test_api_contract_and_sync_snapshot_precedes_network_request(self):
        with TestClient(create_app(self.store, updater=FakeUpdater())) as client:
            path = f"/api/charts/{self.key}/ap-record"
            self.assertEqual(client.put(path, json={"venue": "机厅"}).status_code, 422)
            body = {"venue": "机厅", "request_id": "api-1", "expected_revision": 0}
            self.assertEqual(client.put(path, json=body).status_code, 200)
            self.assertEqual(client.get("/api/ap-records").json()["count"], 1)
            self.assertEqual(
                client.put(path, json={**body, "request_id": "stale"}).status_code, 409
            )
            self.store.save_token("test-not-a-real-token")

            def response(*args, **kwargs):
                chart = self.store.chart(self.key)
                self.save(
                    venue="期间修改", request_id="during-fetch", revision=chart["record_revision"]
                )
                result = unittest.mock.Mock(status_code=200)
                result.json.return_value = {"records": [self.score()]}
                return result

            with patch("backend.app.httpx.get", side_effect=response):
                receipt = client.post("/api/sync/divingfish").json()
            self.assertEqual((receipt["confirmed"], receipt["pending"]), (0, 1))
            chart = self.store.chart(self.key)
            self.assertEqual(
                client.delete(
                    path, params={"expected_revision": chart["record_revision"]}
                ).status_code,
                200,
            )
            self.assertEqual(client.get("/api/ap-records").json()["count"], 0)


if __name__ == "__main__":
    unittest.main()
