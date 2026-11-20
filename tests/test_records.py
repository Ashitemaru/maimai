"""Personal records: chart assembly, AP claims, and Diving-Fish score import."""

import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import httpx

from backend.ap_records import RecordConflictError
from backend.score_sync import RECORDS_ENDPOINT, ScoreSync
from backend.store import Store
from tests.support import TinyStoreCase, add_venue, count

TOKEN = "test-token-for-isolated-fixtures-only"


class ChartAssemblyTests(TinyStoreCase):
    def test_admitted_chart_is_projected_and_source_only_chart_is_not(self):
        chart = self.store.chart(self.key)
        self.assertEqual((chart["id"], chart["difficulty"], chart["song_id"]), (10001, 3, 10001))
        self.assertEqual(chart["jacket_url"], "/jackets/1.png")
        self.assertIn("nickname", chart["_search"])
        self.assertIsNone(self.store.chart("10001:4"))
        # A score on the not-yet-admitted chart is kept, but does not admit it.
        receipt = self.sync(level_index=4)
        self.assertEqual((receipt["matched"], receipt["unmatched"]), (1, 0))
        self.assertIsNone(self.store.chart("10001:4"))

    def test_unadjudicated_chart_in_formal_catalog_is_rejected(self):
        with closing(sqlite3.connect(self.catalog / "catalog.sqlite")) as conn, conn:
            value = json.loads(conn.execute("SELECT data_json FROM charts").fetchone()[0])
            value["admission_status"] = "pending"
            conn.execute("UPDATE charts SET data_json=?", (json.dumps(value),))
        with self.assertRaisesRegex(ValueError, "正式曲库"):
            self.store.reload_catalog()

    def test_provider_ap_without_claim_counts_and_flags_missing_venue(self):
        self.sync("fc")
        self.assertFalse(self.store.chart(self.key)["is_ap"])
        self.sync()
        chart = self.store.chart(self.key)
        self.assertTrue(chart["is_ap"] and chart["is_complete"] and chart["venue_pending"])
        self.assertEqual(chart["venue"], "")


class APRecordTests(TinyStoreCase):
    def test_pending_claim_persists_without_marking_ap(self):
        chart = self.save(venue=f"  {self.venue}  ")
        self.assertEqual(chart["pending_record"]["venue"], self.venue)
        self.assertFalse(chart["is_ap"] or chart["is_complete"])
        restarted = Store(runtime=self.runtime, catalog_dir=self.catalog)
        self.assertEqual(restarted.chart(self.key)["pending_record"], chart["pending_record"])
        self.assertEqual(restarted.ap_records()["count"], 1)

    def test_edit_and_cancel_require_current_revision(self):
        chart = self.save()
        with self.assertRaises(RecordConflictError):
            self.save(request_id="stale")
        edited = self.save(venue="新机厅", request_id="edit", revision=chart["record_revision"])
        with self.assertRaises(RecordConflictError):
            self.store.cancel_ap_record(self.key, chart["record_revision"])
        cleared = self.store.cancel_ap_record(self.key, edited["record_revision"])
        self.assertIsNone(cleared["pending_record"])

    def test_api_saves_lists_rejects_stale_revision_with_409_and_cancels(self):
        client = self.client()
        path = f"/api/charts/{self.key}/ap-record"
        add_venue(self.store, "机厅")
        body = {"venue": "机厅", "request_id": "api-1", "expected_revision": 0}
        self.assertEqual(client.put(path, json={"venue": "机厅"}).status_code, 422)
        unknown = client.put(path, json=body | {"venue": "未知机厅"})
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(client.put(path, json=body).status_code, 200)
        self.assertEqual(client.get("/api/ap-records").json()["count"], 1)
        stale = client.put(path, json=body | {"request_id": "stale", "venue": "机厅"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["chart"]["pending_record"]["venue"], "机厅")
        revision = self.store.chart(self.key)["record_revision"]
        self.assertEqual(
            client.delete(path, params={"expected_revision": revision}).status_code, 200
        )
        self.assertEqual(client.get("/api/ap-records").json()["count"], 0)
        self.assertEqual(
            client.put(path, json=body, headers={"Origin": "https://external.example"}).status_code,
            403,
        )


class ProviderImportTests(TinyStoreCase):
    def test_sync_confirms_claim_and_keeps_ap_evidence(self):
        pending = self.save()["pending_record"]
        self.sync("fcp", achievements=101)
        self.assertEqual(self.store.chart(self.key)["pending_record"]["reason"], "水鱼暂未显示 AP")
        receipt = self.sync("app", achievements=101)
        chart = self.store.chart(self.key)
        self.assertEqual((receipt["confirmed"], receipt["pending"]), (1, 0))
        self.assertEqual((chart["status"], chart["venue"]), ("app", pending["venue"]))
        self.assertIsNone(chart["pending_record"])
        with self.store.db() as conn:
            confirmation = conn.execute("SELECT * FROM ap_confirmations").fetchone()
            self.assertEqual(confirmation["claim_id"], pending["id"])
        # Confirmed AP survives a later missing or reduced remote score.
        self.store.import_provider({"records": []})
        self.assertEqual(self.store.chart(self.key)["status"], "app")
        self.sync("fc")
        chart = self.store.chart(self.key)
        self.assertEqual((chart["status"], chart["conflict"]), ("app", True))
        with self.assertRaisesRegex(ValueError, "已记录机厅"):
            self.save(venue="新机厅", request_id="again", revision=chart["record_revision"])

    def test_malformed_response_rolls_back_the_whole_sync(self):
        self.save()
        self.sync("fc")
        before = self.store.chart(self.key)
        for extra in ({"achievements": float("nan")}, {"fc": "invalid"}, {"dxScore": -1}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.store.import_provider({"records": [self.score(**extra)]})
            self.assertEqual(self.store.chart(self.key), before)
        self.assertEqual(count(self.store, "provider_syncs"), 1)
        self.assertEqual(count(self.store, "ap_confirmations"), 0)


def reply(data, status=200):
    return httpx.Response(status, json=data, request=httpx.Request("GET", RECORDS_ENDPOINT))


class ScoreSyncJobTests(TinyStoreCase):
    def setUp(self):
        super().setUp()
        self.store.save_token(TOKEN)
        self.enterContext(patch("backend.score_sync.JOB_COOLDOWN_SECONDS", 0))

    def run_sync(self, response):
        jobs = ScoreSync(self.store)
        self.addCleanup(jobs.close)
        with patch("backend.score_sync.httpx.get", return_value=response) as get:
            jobs.start()
            jobs._thread.join(timeout=5)
        return jobs.status()["job"], get

    def test_pull_confirms_claim_and_upstream_failure_keeps_scores(self):
        self.save()
        job, get = self.run_sync(reply({"records": [self.score()]}))
        self.assertEqual((job["state"], job["result"]["confirmed"]), ("succeeded", 1))
        self.assertEqual(get.call_args.kwargs["headers"], {"Import-Token": TOKEN})
        self.assertTrue(self.store.chart(self.key)["is_ap"])
        before = self.store.meta("last_divingfish_sync")
        for response, code in (
            (reply({}, status=401), "provider_error"),
            (reply({"records": "broken"}), "invalid_scores"),
        ):
            with self.subTest(code=code):
                job, _ = self.run_sync(response)
                self.assertEqual((job["state"], job["error"]["code"]), ("failed", code))
                self.assertEqual(self.store.meta("last_divingfish_sync"), before)
        self.assertTrue(self.store.chart(self.key)["is_ap"])
