import threading
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from backend.score_sync import RECORDS_ENDPOINT, ScoreSync, ScoreSyncError
from backend.store import Store, SyncBusyError
from backend.test_app import FakeUpdater
from tests import test_chart_store
from tests.support import create_app

TEST_TOKEN = "test-token-for-isolated-fixtures-only"


def score(**overrides):
    return {
        "song_id": 10001,
        "level_index": 3,
        "achievements": 100.9622,
        "fc": "ap",
        "fs": "fsp",
        "dxScore": 1234,
        **overrides,
    }


def response(data, status=200):
    return httpx.Response(status, json=data, request=httpx.Request("GET", RECORDS_ENDPOINT))


class ScoreSyncTests(unittest.TestCase):
    tearDown = test_chart_store.ChartStoreTests.tearDown
    key = "10001:3"

    def setUp(self):
        test_chart_store.ChartStoreTests.setUp(self)
        self.store.save_token(TEST_TOKEN)

    def job(self):
        jobs = ScoreSync(self.store)
        self.addCleanup(jobs.close)
        cooldown = patch("backend.score_sync.JOB_COOLDOWN_SECONDS", 0)
        cooldown.start()
        self.addCleanup(cooldown.stop)
        return jobs

    def finish(self, jobs):
        jobs._thread.join(timeout=4)
        self.assertFalse(jobs._thread.is_alive(), "fixture worker did not finish")
        return jobs.status()["job"]

    def save_claim(self, venue="测试机厅"):
        with self.store.db() as conn:
            conn.execute("INSERT OR IGNORE INTO venues VALUES(?,?,?)", (venue, "fixture", ""))
        return self.store.save_ap_record(
            self.key, {"request_id": "claim", "expected_revision": 0, "venue": venue}
        )

    def run_sync(self, reply):
        jobs = self.job()
        with patch("backend.score_sync.httpx.get", return_value=reply) as get:
            jobs.start()
            job = self.finish(jobs)
        return jobs, job, get

    def test_pull_confirms_pending_claim_and_records_receipt(self):
        self.save_claim()
        jobs, job, get = self.run_sync(response({"records": [score()]}))
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["result"]["confirmed"], 1)
        self.assertEqual(get.call_args.args[0], RECORDS_ENDPOINT)
        self.assertEqual(get.call_args.kwargs["headers"], {"Import-Token": TEST_TOKEN})
        self.assertFalse(get.call_args.kwargs["follow_redirects"])
        chart = self.store.chart(self.key)
        self.assertTrue(chart["is_ap"])
        self.assertEqual(chart["venue"], "测试机厅")
        self.assertEqual(
            jobs.status()["divingfish"]["last_sync"]["sync_id"], job["result"]["sync_id"]
        )
        self.assertNotIn(TEST_TOKEN, str(jobs.status()))

    def test_ap_without_claim_counts_and_waits_for_venue(self):
        _, job, _ = self.run_sync(response({"records": [score(fc="app", achievements=101)]}))
        chart = self.store.chart(self.key)
        self.assertEqual((chart["is_ap"], chart["status"]), (True, "app"))
        self.assertTrue(chart["venue_pending"])
        self.assertEqual(job["result"]["venue_pending"], 1)

    def test_observed_ap_survives_later_missing_or_reduced_record(self):
        self.run_sync(response({"records": [score()]}))
        self.run_sync(response({"records": [score(fc="", achievements=99.5)]}))
        self.assertTrue(self.store.chart(self.key)["is_ap"])
        self.run_sync(response({"records": []}))
        self.assertTrue(self.store.chart(self.key)["is_ap"])

    def test_upstream_failures_keep_scores_and_report_safely(self):
        self.run_sync(response({"records": [score()]}))
        before = self.store.meta("last_divingfish_sync")
        cases = [
            (response({}, status=401), "provider_error"),
            (response({"records": "broken"}), "invalid_scores"),
            (response({"records": [score(level_index=7)]}), "invalid_scores"),
        ]
        for reply, code in cases:
            with self.subTest(code=code):
                _, job, _ = self.run_sync(reply)
                self.assertEqual((job["state"], job["error"]["code"]), ("failed", code))
                self.assertEqual(self.store.meta("last_divingfish_sync"), before)
        jobs = self.job()
        with patch("backend.score_sync.httpx.get", side_effect=httpx.ConnectError("boom")):
            jobs.start()
            self.assertEqual(self.finish(jobs)["error"]["code"], "network_error")
        self.assertTrue(self.store.chart(self.key)["is_ap"])

    def test_token_change_during_pull_discards_response(self):
        entered, release = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return response({"records": [score()]})

        jobs = self.job()
        with patch("backend.score_sync.httpx.get", side_effect=slow):
            jobs.start()
            self.assertTrue(entered.wait(5))
            self.store.save_token("replacement-token")
            release.set()
            job = self.finish(jobs)
        self.assertEqual(job["error"]["code"], "token_changed")
        self.assertIsNone(self.store.meta("last_divingfish_sync"))
        self.assertFalse(self.store.chart(self.key)["is_ap"])

    def test_requires_token_and_rejects_overlap(self):
        self.store.save_token("")
        with self.assertRaises(ScoreSyncError) as failure:
            self.job().start()
        self.assertEqual(failure.exception.code, "no_token")
        self.store.save_token(TEST_TOKEN)
        other = Store(runtime=self.runtime, catalog_dir=self.catalog)
        with other.sync_lease():
            with self.assertRaises(ScoreSyncError) as failure:
                self.job().start()
            self.assertEqual(failure.exception.code, "sync_busy")
        with self.store.sync_lease():
            with self.assertRaises(SyncBusyError), other.sync_lease():
                pass

    def test_shutdown_waits_for_worker_and_rejects_new_jobs(self):
        jobs = self.job()
        worker = Mock(spec=threading.Thread)
        jobs._thread = worker
        jobs.close()
        worker.join.assert_called_once_with(timeout=40)
        with self.assertRaises(ScoreSyncError) as failure:
            jobs.start()
        self.assertEqual(failure.exception.code, "shutting_down")

    def test_api_starts_job_and_reports_status(self):
        app = create_app(self.store, updater=FakeUpdater())
        with (
            patch("backend.score_sync.httpx.get", return_value=response({"records": [score()]})),
            TestClient(app) as client,
        ):
            accepted = client.post("/api/sync/divingfish")
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(accepted.json()["kind"], "divingfish")
            self.finish(app.state.score_sync)
            status = client.get("/api/sync/status").json()
            self.assertEqual(status["job"]["state"], "succeeded")
            self.assertTrue(status["divingfish"]["configured"])
            self.assertEqual(status["divingfish"]["last_sync"]["matched"], 1)
            self.store.save_token("")
            refused = client.post("/api/sync/divingfish")
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(refused.json()["detail"]["code"], "no_token")


if __name__ == "__main__":
    unittest.main()
