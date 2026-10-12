import copy
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from backend.ap_records import ScoreAccountError, ScoreSourceError
from backend.score_sync import (
    UPLOAD_ENDPOINT,
    ScoreSync,
    ScoreSyncError,
    fetch_wahlap_scores,
    upload_records,
)
from backend.store import Store, SyncBusyError
from backend.test_app import FakeUpdater
from tests import test_chart_store
from tests.support import create_app

TEST_QR = "SGWCMAID123456789012TESTONLYTOKEN"
TEST_TOKEN = "test-token-for-isolated-fixtures-only"
AVAILABLE = {"available": True, "reason": None}


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


def payload(**overrides):
    return {
        "records": [score()],
        "player_id": "123456",
        "logout_confirmed": True,
        "source": "wahlap",
        **overrides,
    }


def music_data():
    return [{"id": "10001", "title": "Example", "type": "DX", "charts": [{}, {}, {}, {}]}]


def response(data):
    return httpx.Response(200, json=data, request=httpx.Request("GET", "https://example.invalid"))


class ScoreSyncTests(unittest.TestCase):
    setUp = test_chart_store.ChartStoreTests.setUp
    tearDown = test_chart_store.ChartStoreTests.tearDown
    key = "10001:3"

    def import_game(self, records=None, account_id="123456"):
        return self.store.import_provider(
            {"records": records if records is not None else [score()]},
            source="wahlap",
            account_id=account_id,
            outcome={"logout_confirmed": True, "logout_status": "confirmed", "notice": None},
        )

    def save_claim(self, request="claim", revision=0, venue="测试机厅"):
        with self.store.db() as conn:
            conn.execute("INSERT OR IGNORE INTO venues VALUES(?,?,?)", (venue, "fixture", ""))
        return self.store.save_ap_record(
            self.key,
            {"request_id": request, "expected_revision": revision, "venue": venue},
        )

    def state(self):
        with self.store.db() as conn:
            return {
                name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")]
                for name in (
                    "provider",
                    "provider_details",
                    "provider_unmatched",
                    "provider_syncs",
                    "ap_pending",
                    "ap_confirmations",
                    "ap_evidence",
                    "metadata",
                    "events",
                    "score_sync_sources",
                )
            }

    def job(self):
        jobs = ScoreSync(self.store)
        self.addCleanup(jobs.close)
        self.addCleanup(patch.stopall)
        patch("backend.score_sync.wahlap_available", return_value=AVAILABLE).start()
        patch("backend.score_sync.JOB_COOLDOWN_SECONDS", 0).start()
        return jobs

    def finish(self, jobs):
        jobs._thread.join(timeout=4)
        self.assertFalse(jobs._thread.is_alive(), "fixture worker did not finish")
        return jobs.status()["job"]

    def test_game_matches_official_id_without_divingfish_mapping_and_keeps_source(self):
        catalog, _ = self.store._catalog_snapshot
        self.store._catalog_snapshot = (catalog, {})
        self.save_claim()
        receipt = self.import_game()
        self.assertEqual((receipt["matched"], receipt["confirmed"]), (1, 1))
        chart = self.store.chart(self.key)
        self.assertEqual((chart["venue"], chart["provider_source"]), ("测试机厅", "wahlap"))
        self.assertEqual((chart["provider_fs"], chart["provider_dx_score"]), ("fsp", 1234))
        with self.store.db() as conn:
            source = conn.execute("SELECT * FROM score_sync_sources").fetchone()
            self.assertEqual(source["source"], "wahlap")
            self.assertEqual(source["sync_id"], receipt["sync_id"])
            self.assertNotEqual(source["account_hash"], "123456")
        restarted = Store(runtime=self.runtime, catalog_dir=self.catalog)
        self.assertEqual(restarted.meta("last_wahlap_sync"), receipt)
        self.assertEqual(restarted.chart(self.key)["provider_source"], "wahlap")

    def test_account_switch_and_legacy_pull_cannot_overwrite_game_snapshot(self):
        self.save_claim()
        self.import_game()
        before = self.state()
        with self.assertRaises(ScoreAccountError):
            self.import_game([score(fc="")], account_id="999999")
        with self.assertRaises(ScoreSourceError):
            self.store.import_provider({"records": [score(fc="")]})
        self.assertEqual(self.state(), before)

    def test_invalid_snapshot_is_atomic_and_cannot_bind_account(self):
        self.save_claim()
        before = self.state()
        malformed = [
            [],
            [score(), score()],
            [score(), score(song_id=10002, achievements=float("nan"))],
            [score(dxScore=None)],
            [score(dxScore=True)],
            [score(fs="bogus")],
            [score(fc="unknown")],
            [score(achievements="100.9")],
            [{key: value for key, value in score().items() if key != "fs"}],
            [score(title="untrusted extra field")],
        ]
        for records in malformed:
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.import_game(records)
            self.assertEqual(self.state(), before)
        for account in (None, 123, "", "0", "-1", "１２３", "1" * 21):
            with self.subTest(account=account), self.assertRaises(ScoreAccountError):
                self.import_game(account_id=account)
            self.assertEqual(self.state(), before)

    def test_unmatched_and_utage_remain_in_full_snapshot_without_fabricated_ap(self):
        records = [score(), score(song_id=120000, level_index=5, achievements=120)]
        receipt = self.import_game(records)
        self.assertEqual((receipt["records"], receipt["matched"], receipt["unmatched"]), (2, 1, 1))
        with self.store.db() as conn:
            raw = conn.execute("SELECT records_json FROM provider_syncs").fetchone()[0]
            self.assertEqual(json.loads(raw), records)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ap_evidence").fetchone()[0], 1)

    def test_observed_ap_survives_missing_and_reduced_game_records(self):
        self.save_claim()
        self.import_game([score(fc="app", achievements=101)])
        self.import_game([score(song_id=999999)])
        self.assertEqual(self.store.chart(self.key)["status"], "app")
        self.import_game([score(fc="fc", achievements=99)])
        chart = self.store.chart(self.key)
        self.assertEqual((chart["status"], chart["venue"]), ("app", "测试机厅"))
        self.assertTrue(chart["conflict"])

    def test_job_freezes_eligible_pending_and_redacts_private_context(self):
        chart = self.save_claim()
        started, release = threading.Event(), threading.Event()

        def fetch(_):
            started.set()
            self.assertTrue(release.wait(3))
            return payload()

        jobs = self.job()
        with patch("backend.score_sync.fetch_wahlap_scores", side_effect=fetch):
            accepted = jobs.start_wahlap(TEST_QR)
            self.assertEqual(accepted["state"], "running")
            self.assertTrue(started.wait(2))
            with self.assertRaises(ScoreSyncError) as busy:
                jobs.start_wahlap(TEST_QR)
            self.assertEqual(busy.exception.status, 409)
            edited = self.save_claim("edit", chart["record_revision"], "另一机厅")
            release.set()
            result = self.finish(jobs)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["confirmed"], 0)
        self.assertEqual(self.store.chart(self.key)["pending_record"], edited["pending_record"])
        serialized = json.dumps(jobs.status())
        for secret in (TEST_QR, "player_id", "account_hash", '"123456"'):
            self.assertNotIn(secret, serialized)

    def test_unconfirmed_logout_preserves_scores_but_never_claims_verified(self):
        jobs = self.job()
        with patch(
            "backend.score_sync.fetch_wahlap_scores",
            return_value=payload(logout_confirmed=False, logout_status="failed", notice=TEST_QR),
        ):
            jobs.start_wahlap(TEST_QR)
            result = self.finish(jobs)
        self.assertEqual(result["state"], "succeeded")
        status = jobs.status()
        self.assertTrue(status["wahlap"]["has_snapshot"])
        self.assertFalse(status["wahlap"]["verified"])
        self.assertEqual(status["wahlap"]["last_sync"]["logout_status"], "failed")
        self.assertNotIn(TEST_QR, json.dumps(status))
        self.assertTrue(self.store.chart(self.key)["is_ap"])

    def test_worker_failure_is_safe_and_persists_nothing(self):
        jobs = self.job()
        before = self.state()
        with patch("backend.score_sync.fetch_wahlap_scores", side_effect=RuntimeError(TEST_QR)):
            jobs.start_wahlap(TEST_QR)
            result = self.finish(jobs)
        self.assertEqual(result["state"], "failed")
        self.assertNotIn(TEST_QR, json.dumps(jobs.status()))
        self.assertEqual(self.state(), before)

    def test_score_failure_keeps_logout_failure_visible(self):
        from backend.wahlap import WahlapError

        error = WahlapError("invalid_scores")
        error.logout_status = "failed"
        with patch("backend.wahlap.fetch_scores", side_effect=error):
            with self.assertRaises(ScoreSyncError) as failure:
                fetch_wahlap_scores(TEST_QR)
        self.assertIn("成绩校验失败", failure.exception.message)
        self.assertIn("退出请求未成功", failure.exception.message)
        self.assertNotIn(TEST_QR, str(failure.exception))

    def test_status_restart_has_receipt_but_no_job_or_credential(self):
        jobs = self.job()
        with patch("backend.score_sync.fetch_wahlap_scores", return_value=payload()):
            jobs.start_wahlap(TEST_QR)
            self.finish(jobs)
        restarted = ScoreSync(Store(runtime=self.runtime, catalog_dir=self.catalog))
        self.addCleanup(restarted.close)
        status = restarted.status()
        self.assertIsNone(status["job"])
        self.assertTrue(status["wahlap"]["verified"])
        for path in self.runtime.glob("*"):
            if path.is_file():
                self.assertNotIn(TEST_QR.encode(), path.read_bytes())

    def test_cross_store_lease_rejects_overlapping_job(self):
        other = Store(runtime=self.runtime, catalog_dir=self.catalog)
        with self.store.sync_lease():
            with self.assertRaises(SyncBusyError), other.sync_lease():
                pass
        with other.sync_lease():
            pass

    def test_shutdown_drains_bounded_worker_and_rejects_new_jobs(self):
        jobs = self.job()
        worker = Mock(spec=threading.Thread)
        jobs._thread = worker
        jobs.close()
        worker.join.assert_called_once_with(timeout=190)
        with self.assertRaises(ScoreSyncError) as failure:
            jobs.start_wahlap(TEST_QR)
        self.assertEqual(failure.exception.code, "shutting_down")

    def test_unavailable_and_invalid_qr_are_safe_without_job(self):
        jobs = self.job()
        with patch("backend.score_sync.wahlap_available", return_value={"available": False}):
            with self.assertRaises(ScoreSyncError) as failure:
                jobs.start_wahlap(TEST_QR)
            self.assertEqual(failure.exception.status, 503)
        with self.assertRaises(ScoreSyncError) as failure:
            jobs.start_wahlap("secret-not-a-qr")
        self.assertEqual(failure.exception.status, 422)
        self.assertNotIn("secret-not-a-qr", str(failure.exception))
        self.assertIsNone(jobs.status()["job"])

    def test_api_validation_and_legacy_pull_are_redacted_and_no_network(self):
        self.import_game()
        with TestClient(create_app(self.store, updater=FakeUpdater())) as client:
            for body in ({"qr_text": {"secret": TEST_QR}}, {"qr_text": TEST_QR, "extra": TEST_QR}):
                result = client.post("/api/sync/wahlap", json=body)
                self.assertEqual(result.status_code, 422)
                self.assertNotIn(TEST_QR, result.text)
            with patch("backend.app.httpx.get") as get:
                result = client.post("/api/sync/divingfish")
            self.assertEqual(result.status_code, 409)
            get.assert_not_called()

    def prepare_upload(self, records=None):
        self.import_game(records)
        self.store.save_token(TEST_TOKEN)
        return self.job()

    def test_upload_uses_explicit_snapshot_and_reports_skips_without_touching_local_scores(self):
        records = [score(), score(song_id=999999, level_index=5, achievements=120)]
        jobs = self.prepare_upload(records)
        before = self.state()
        with patch("backend.score_sync.httpx.get", return_value=response(music_data())) as get:
            with patch(
                "backend.score_sync.httpx.post",
                return_value=response({"message": "更新成功", "updates": 1, "creates": 0}),
            ) as post:
                jobs.start_upload()
                result = self.finish(jobs)
        self.assertEqual(result["state"], "succeeded")
        receipt = result["result"]
        self.assertEqual((receipt["uploaded_count"], receipt["skipped_count"]), (1, 1))
        self.assertEqual(receipt["snapshot_records"], 2)
        self.assertEqual(post.call_args.args, (UPLOAD_ENDPOINT,))
        self.assertEqual(
            post.call_args.kwargs["json"], [{**score(), "title": "Example", "type": "DX"}]
        )
        self.assertEqual(post.call_args.kwargs["headers"], {"Import-Token": TEST_TOKEN})
        self.assertFalse(post.call_args.kwargs["follow_redirects"])
        self.assertNotIn("headers", get.call_args.kwargs)
        after = self.state()
        before.pop("metadata")
        after.pop("metadata")
        self.assertEqual(after, before)
        self.assertEqual(self.store.meta("last_divingfish_upload"), receipt)

    def test_slow_upload_does_not_block_status_or_settings(self):
        jobs = self.prepare_upload()
        started, release = threading.Event(), threading.Event()

        def post(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(3))
            return response({"message": "更新成功", "updates": 1, "creates": 0})

        with patch("backend.score_sync.httpx.get", return_value=response(music_data())):
            with patch("backend.score_sync.httpx.post", side_effect=post):
                jobs.start_upload()
                self.assertTrue(started.wait(2))
                at = time.monotonic()
                status = jobs.status()
                self.assertLess(time.monotonic() - at, 0.5)
                self.assertEqual(status["job"]["stage"], "uploading")
                self.store.save_token("replacement-token")
                release.set()
                result = self.finish(jobs)
        self.assertEqual(result["state"], "succeeded")

    def test_token_change_while_preparing_upload_stops_before_post(self):
        jobs = self.prepare_upload()

        def get(*args, **kwargs):
            self.store.save_token("replacement-token")
            return response(music_data())

        with patch("backend.score_sync.httpx.get", side_effect=get):
            with patch("backend.score_sync.httpx.post") as post:
                jobs.start_upload()
                result = self.finish(jobs)
        post.assert_not_called()
        self.assertEqual(result["error"]["code"], "token_changed")
        self.assertFalse(self.store.meta("last_divingfish_upload")["ok"])

    def test_failed_or_partial_upload_keeps_local_snapshot_and_safe_receipt(self):
        jobs = self.prepare_upload()
        original = self.store.meta("last_wahlap_sync")
        replies = [
            response({"message": "更新成功", "updates": 0, "creates": 0}),
            RuntimeError(TEST_TOKEN),
        ]
        for reply in replies:
            with self.subTest(reply=type(reply).__name__):
                with patch("backend.score_sync.httpx.get", return_value=response(music_data())):
                    with patch("backend.score_sync.httpx.post", side_effect=[reply]):
                        jobs.start_upload()
                        result = self.finish(jobs)
                self.assertEqual(result["state"], "failed")
                self.assertEqual(self.store.meta("last_wahlap_sync"), original)
                self.assertTrue(self.store.chart(self.key)["is_ap"])
                receipt = self.store.meta("last_divingfish_upload")
                self.assertFalse(receipt["ok"])
                self.assertNotIn(TEST_TOKEN, json.dumps(receipt))

    def test_upload_mapping_rejects_all_unknown_and_ambiguous_title(self):
        for catalog in ([], {"invalid": True}):
            with self.assertRaises(ScoreSyncError):
                upload_records([score()], catalog)
        catalog = music_data()
        duplicate = copy.deepcopy(catalog[0])
        duplicate["id"] = "10002"
        for records in ([score()], [score(), score(song_id=10002)]):
            with self.assertRaises(ScoreSyncError) as failure:
                upload_records(records, [*catalog, duplicate])
            self.assertEqual(failure.exception.code, "upload_ambiguous")

    def test_api_starts_job_and_returns_public_receipt(self):
        with patch("backend.score_sync.wahlap_available", return_value=AVAILABLE):
            with patch("backend.score_sync.fetch_wahlap_scores", return_value=payload()):
                app = create_app(self.store, updater=FakeUpdater())
                with TestClient(app) as client:
                    accepted = client.post("/api/sync/wahlap", json={"qr_text": TEST_QR})
                    self.assertEqual(accepted.status_code, 202)
                    self.assertEqual(accepted.json()["kind"], "wahlap")
                    self.finish(app.state.score_sync)
                    status = client.get("/api/sync/status")
                    self.assertEqual(status.status_code, 200)
                    self.assertEqual(status.json()["job"]["state"], "succeeded")
                    self.assertTrue(status.json()["wahlap"]["verified"])
                    self.assertNotIn(TEST_QR, status.text)

    def test_upload_requires_explicit_token_and_successful_game_snapshot(self):
        jobs = self.job()
        with self.assertRaises(ScoreSyncError) as failure:
            jobs.start_upload()
        self.assertEqual(failure.exception.code, "no_snapshot")
        self.import_game()
        with self.assertRaises(ScoreSyncError) as failure:
            jobs.start_upload()
        self.assertEqual(failure.exception.code, "no_token")


if __name__ == "__main__":
    unittest.main()
