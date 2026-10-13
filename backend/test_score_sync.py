import copy
import json
import os
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from backend.ap_records import ScoreAccountError, ScoreSourceError
from backend.score_sync import (
    RECORDS_ENDPOINT,
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
OUTSIDE_MAINTENANCE = datetime.fromisoformat("2026-10-13T12:00:00+08:00")
DURING_MAINTENANCE = datetime.fromisoformat("2026-10-13T05:30:00+08:00")


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


def upload_get(before=None, after=None):
    replies = iter([{"records": before or []}, {"records": after or [score()]}])

    def get(url, **kwargs):
        if url.endswith("/music_data"):
            return response(music_data())
        if url != RECORDS_ENDPOINT:
            raise AssertionError("unexpected read endpoint")
        return response(next(replies))

    return get


class ScoreSyncTests(unittest.TestCase):
    tearDown = test_chart_store.ChartStoreTests.tearDown
    key = "10001:3"

    def setUp(self):
        test_chart_store.ChartStoreTests.setUp(self)
        clock = patch("backend.score_sync.utc_now", return_value=OUTSIDE_MAINTENANCE)
        clock.start()
        self.addCleanup(clock.stop)

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

    def job(self, *, clock=None):
        jobs = ScoreSync(self.store, clock=clock)
        self.addCleanup(jobs.close)
        self.addCleanup(patch.stopall)
        patch("backend.score_sync.wahlap_available", return_value=AVAILABLE).start()
        patch("backend.score_sync.JOB_COOLDOWN_SECONDS", 0).start()
        return jobs

    def finish(self, jobs):
        jobs._thread.join(timeout=4)
        self.assertFalse(jobs._thread.is_alive(), "fixture worker did not finish")
        return jobs.status()["job"]

    def test_network_probe_preserves_scores_and_does_not_create_a_sync_job(self):
        jobs = self.job()
        self.save_claim()
        before = self.state()
        result = {"ok": True, "probe": "network", "account_access": False}
        with patch("backend.wahlap.probe_network", return_value=result) as probe:
            self.assertEqual(jobs.probe_wahlap(), result)
        probe.assert_called_once_with()
        self.assertEqual(self.state(), before)
        self.assertIsNone(jobs.status()["job"])
        self.assertFalse(jobs.status()["wahlap"]["checking"])
        self.assertFalse(jobs.status()["wahlap"]["verified"])

    def test_network_probe_serializes_sync_and_other_probes_without_blocking_status(self):
        jobs = self.job()
        started, release = threading.Event(), threading.Event()
        result = []

        def probe():
            started.set()
            self.assertTrue(release.wait(3))
            return {"ok": True, "probe": "network", "account_access": False}

        with patch("backend.wahlap.probe_network", side_effect=probe):
            thread = threading.Thread(target=lambda: result.append(jobs.probe_wahlap()))
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                with patch("backend.score_sync.wahlap_available") as imports_probe:
                    self.assertTrue(jobs.status()["wahlap"]["checking"])
                imports_probe.assert_not_called()
                for action in (jobs.probe_wahlap, lambda: jobs.start_wahlap(TEST_QR)):
                    with self.assertRaises(ScoreSyncError) as failure:
                        action()
                    self.assertEqual(failure.exception.code, "sync_busy")
                with self.assertRaises(SyncBusyError), self.store.sync_lease():
                    self.fail("Legacy score sync must not overlap the connectivity check")
            finally:
                release.set()
                thread.join(timeout=3)
        self.assertEqual(len(result), 1)
        self.assertFalse(jobs.status()["wahlap"]["checking"])
        with self.store.sync_lease():
            pass

    def test_network_probe_refuses_running_sync_maintenance_shutdown_and_cooldown(self):
        jobs = self.job()
        with patch("backend.wahlap.probe_network") as probe:
            jobs._job = {"state": "running"}
            with self.assertRaises(ScoreSyncError) as failure:
                jobs.probe_wahlap()
            self.assertEqual(failure.exception.code, "sync_busy")
            jobs._job = None
            jobs._clock = lambda: DURING_MAINTENANCE
            with self.assertRaises(ScoreSyncError) as failure:
                jobs.probe_wahlap()
            self.assertEqual(failure.exception.code, "maintenance")
            jobs._clock = lambda: OUTSIDE_MAINTENANCE
            jobs._last_started = time.monotonic()
            with patch("backend.score_sync.JOB_COOLDOWN_SECONDS", 3):
                with self.assertRaises(ScoreSyncError) as failure:
                    jobs.probe_wahlap()
                self.assertEqual(failure.exception.code, "sync_cooldown")
            jobs.close()
            with self.assertRaises(ScoreSyncError) as failure:
                jobs.probe_wahlap()
            self.assertEqual(failure.exception.code, "shutting_down")
        probe.assert_not_called()

    def test_network_probe_error_releases_lease_and_preserves_safe_diagnostics(self):
        from backend.wahlap import WahlapError

        jobs = self.job()
        before = self.state()
        with patch(
            "backend.wahlap.probe_network",
            side_effect=WahlapError(
                "upstream_empty_response", stage="configuration", http_status=200
            ),
        ):
            with self.assertRaises(ScoreSyncError) as failure:
                jobs.probe_wahlap()
        self.assertEqual(
            failure.exception.detail(),
            {
                "code": "upstream_empty_response",
                "message": "华立返回空响应，请检查取分代理出口",
                "stage": "configuration",
                "http_status": 200,
            },
        )
        self.assertFalse(jobs.status()["wahlap"]["checking"])
        self.assertEqual(self.state(), before)
        with self.store.sync_lease():
            pass

    def test_network_probe_requires_session_and_same_origin(self):
        from backend.auth import hash_master_key

        key = "synthetic-network-probe-master-key"
        with (
            patch.dict(
                os.environ,
                {"MAIMAI_AUTH_DISABLED": "0", "MAIMAI_MASTER_KEY_HASH": hash_master_key(key)},
            ),
            patch("backend.wahlap.probe_network") as probe,
            TestClient(
                create_app(self.store, updater=FakeUpdater()), base_url="https://testserver"
            ) as client,
        ):
            self.assertEqual(client.post("/api/sync/wahlap/probe").status_code, 401)
            self.assertEqual(
                client.post("/api/auth/login", json={"master_key": key}).status_code, 200
            )
            self.assertEqual(
                client.post(
                    "/api/sync/wahlap/probe", headers={"Origin": "https://other.invalid"}
                ).status_code,
                403,
            )
        probe.assert_not_called()

    def test_network_probe_api_success_and_maintenance(self):
        result = {"ok": True, "probe": "network", "account_access": False}
        with (
            patch("backend.wahlap.probe_network", return_value=result) as probe,
            TestClient(create_app(self.store, updater=FakeUpdater())) as client,
        ):
            response = client.post("/api/sync/wahlap/probe")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), result)
            self.assertEqual(response.headers["cache-control"], "no-store")
            probe.assert_called_once_with()
            with patch("backend.score_sync.utc_now", return_value=DURING_MAINTENANCE):
                client.app.state.score_sync._clock = lambda: DURING_MAINTENANCE
                response = client.post("/api/sync/wahlap/probe")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "5400")
            self.assertEqual(response.json()["detail"]["code"], "maintenance")
            probe.assert_called_once_with()

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
            [score(), score(song_id=10002, level_index=10)],
            [score(), score(song_id=120000, level_index=6)],
            [score(), score(song_id=120000, level_index=True)],
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
        records = [
            score(),
            score(song_id=120000, level_index=5, achievements=120),
            score(song_id=111359, level_index=10, achievements=200.6284),
        ]
        receipt = self.import_game(records)
        self.assertEqual((receipt["records"], receipt["matched"], receipt["unmatched"]), (3, 1, 2))
        with self.store.db() as conn:
            raw = conn.execute("SELECT records_json FROM provider_syncs").fetchone()[0]
            self.assertEqual(json.loads(raw), records)
            unmatched = conn.execute(
                "SELECT raw_json FROM provider_unmatched WHERE source_key='111359:10'"
            ).fetchone()
            self.assertEqual(json.loads(unmatched[0]), records[2])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ap_evidence").fetchone()[0], 1)

    def test_game_utage_level_does_not_change_divingfish_contract(self):
        before = self.state()
        with self.assertRaises(ValueError):
            self.store.import_provider({"records": [score(song_id=111359, level_index=10)]})
        self.assertEqual(self.state(), before)

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

    def test_pagination_failure_keeps_safe_diagnostics_without_importing_scores(self):
        from backend.wahlap import WahlapError

        error = WahlapError("pagination_invalid", stage="scores", pagination_reason="cursor_range")
        error.logout_status = "confirmed"
        error.logout_confirmed = True
        jobs = self.job()
        before = self.state()
        with patch("backend.wahlap.fetch_scores", side_effect=error):
            jobs.start_wahlap(TEST_QR)
            result = self.finish(jobs)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["stage"], "scores")
        self.assertEqual(result["error"]["pagination_reason"], "cursor_range")
        self.assertEqual(result["error"]["logout_status"], "confirmed")
        self.assertTrue(result["error"]["logout_confirmed"])
        self.assertNotIn(TEST_QR, json.dumps(jobs.status()))
        self.assertEqual(self.state(), before)

    def test_network_error_after_login_keeps_unconfirmed_logout_visible(self):
        from backend.wahlap import WahlapError

        for code in (
            "upstream_timeout",
            "upstream_transport_error",
            "upstream_empty_response",
            "upstream_decode_error",
            "upstream_http_error",
        ):
            with self.subTest(code=code):
                error = WahlapError(code, stage="scores")
                with patch("backend.wahlap.fetch_scores", side_effect=error):
                    with self.assertRaises(ScoreSyncError) as failure:
                        fetch_wahlap_scores(TEST_QR)
                self.assertIn("退出会话尚未确认", failure.exception.message)
                self.assertEqual(failure.exception.detail()["logout_status"], "unverified")
                error.stage = "configuration"
                error.logout_status = "not_attempted"
                with patch("backend.wahlap.fetch_scores", side_effect=error):
                    with self.assertRaises(ScoreSyncError) as failure:
                        fetch_wahlap_scores(TEST_QR)
                self.assertNotIn("退出会话尚未确认", failure.exception.message)

    def test_score_error_diagnostics_exclude_unknown_values(self):
        error = ScoreSyncError(
            502,
            "pagination_invalid",
            "成绩分页异常",
            pagination_reason=TEST_QR,
            logout_status=TEST_QR,
        )
        self.assertEqual(error.detail(), {"code": "pagination_invalid", "message": "成绩分页异常"})
        for invalid in ([], {}, True):
            error.pagination_reason = invalid
            error.logout_status = invalid
            self.assertNotIn("pagination_reason", error.detail())
            self.assertNotIn("logout_status", error.detail())
        error.code = "invalid_scores"
        error.pagination_reason = "cursor_range"
        self.assertNotIn("pagination_reason", error.detail())

    def test_score_error_does_not_claim_unconfirmed_logout(self):
        error = ScoreSyncError(
            502,
            "pagination_invalid",
            "成绩分页异常",
            logout_status="confirmed",
            logout_confirmed=False,
        )
        self.assertEqual(error.detail()["logout_status"], "unverified")
        self.assertFalse(error.detail()["logout_confirmed"])

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
        with patch("backend.score_sync.httpx.get", side_effect=upload_get()) as get:
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
        self.assertNotIn("headers", get.call_args_list[0].kwargs)
        self.assertEqual(get.call_args.kwargs["headers"], {"Import-Token": TEST_TOKEN})
        self.assertEqual(receipt["verification_state"], "verified")
        self.assertEqual(receipt["verified_count"], 1)
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

        with patch("backend.score_sync.httpx.get", side_effect=upload_get()):
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
                with patch("backend.score_sync.httpx.get", side_effect=upload_get()):
                    with patch("backend.score_sync.httpx.post", side_effect=[reply]):
                        jobs.start_upload()
                        result = self.finish(jobs)
                self.assertEqual(result["state"], "failed")
                self.assertEqual(self.store.meta("last_wahlap_sync"), original)
                self.assertTrue(self.store.chart(self.key)["is_ap"])
                receipt = self.store.meta("last_divingfish_upload")
                self.assertFalse(receipt["ok"])
                self.assertNotIn(TEST_TOKEN, json.dumps(receipt))

    def test_upload_keeps_native_utage_level_out_of_divingfish_indices(self):
        utage = score(song_id=111359, level_index=10, achievements=200.6284)
        catalog = [
            *music_data(),
            {"id": "111359", "title": "Utage", "type": "DX", "charts": [{}] * 11},
        ]
        uploaded, skipped = upload_records([score(), utage], catalog)
        self.assertEqual([item["song_id"] for item in uploaded], [10001])
        self.assertEqual(
            [(item["song_id"], item["level_index"]) for item in skipped], [(111359, 10)]
        )
        self.assertEqual(utage["level_index"], 10)

    def test_upload_noop_reads_remote_bests_without_posting(self):
        jobs = self.prepare_upload()
        remote = score(achievements=101, fc="app", fs="fsdp", dxScore=2000)
        with (
            patch("backend.score_sync.httpx.get", side_effect=upload_get(before=[remote])) as get,
            patch("backend.score_sync.httpx.post") as post,
        ):
            jobs.start_upload()
            result = self.finish(jobs)
        self.assertEqual(result["state"], "succeeded")
        receipt = result["result"]
        self.assertEqual(receipt["uploaded_count"], 0)
        self.assertEqual(receipt["preserved_count"], 1)
        self.assertEqual(receipt["verification_state"], "verified")
        self.assertFalse(receipt["write_attempted"])
        self.assertFalse(receipt["upload_confirmed"])
        self.assertEqual(get.call_count, 2)
        post.assert_not_called()

    def test_upload_mixed_improvements_preserve_remote_fields_and_check_other_records(self):
        jobs = self.prepare_upload()
        old = score(achievements=101, fc="fc", fs="fsdp", dxScore=1000)
        extra = score(song_id=9999)
        merged = score(achievements=101, fs="fsdp")
        with (
            patch(
                "backend.score_sync.httpx.get",
                side_effect=upload_get(before=[old, extra], after=[merged, extra]),
            ),
            patch(
                "backend.score_sync.httpx.post",
                return_value=response({"message": "更新成功", "updates": 1, "creates": 0}),
            ) as post,
        ):
            jobs.start_upload()
            result = self.finish(jobs)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(
            post.call_args.kwargs["json"], [{**merged, "title": "Example", "type": "DX"}]
        )
        self.assertEqual(result["result"]["verified_count"], 2)
        self.assertEqual(result["result"]["outside_upload_count"], 1)
        self.assertEqual(
            self.store.chart(self.key)["provider_achievement"], score()["achievements"]
        )

    def test_unavailable_or_invalid_preflight_never_posts(self):
        jobs = self.prepare_upload()
        failures = [
            httpx.ReadTimeout(TEST_TOKEN),
            response({"records": [score(), score()]}),
            response({"records": [{"song_id": 10001}]}),
            response({"not_records": []}),
        ]
        for failure in failures:
            with (
                self.subTest(failure=type(failure).__name__),
                patch(
                    "backend.score_sync.httpx.get", side_effect=[response(music_data()), failure]
                ),
                patch("backend.score_sync.httpx.post") as post,
            ):
                jobs.start_upload()
                result = self.finish(jobs)
                self.assertEqual(result["error"]["code"], "upload_preflight_failed")
                receipt = self.store.meta("last_divingfish_upload")
                self.assertFalse(receipt["write_attempted"])
                self.assertEqual(receipt["verification_state"], "not_started")
                self.assertNotIn(TEST_TOKEN, json.dumps(receipt))
                post.assert_not_called()

    def test_acknowledged_upload_with_unavailable_readback_is_not_success(self):
        jobs = self.prepare_upload()
        with (
            patch(
                "backend.score_sync.httpx.get",
                side_effect=[
                    response(music_data()),
                    response({"records": []}),
                    httpx.ReadTimeout(TEST_TOKEN),
                ],
            ),
            patch(
                "backend.score_sync.httpx.post",
                return_value=response({"message": "更新成功", "updates": 0, "creates": 1}),
            ) as post,
        ):
            jobs.start_upload()
            result = self.finish(jobs)
        self.assertEqual(result["error"]["code"], "upload_verification_unavailable")
        receipt = self.store.meta("last_divingfish_upload")
        self.assertTrue(receipt["write_attempted"])
        self.assertTrue(receipt["upload_confirmed"])
        self.assertEqual(receipt["verification_state"], "unverified")
        self.assertNotIn(TEST_TOKEN, json.dumps(receipt))
        post.assert_called_once()

    def test_acknowledged_upload_detects_missing_or_degraded_outside_records(self):
        jobs = self.prepare_upload()
        extra = score(song_id=9999)
        for after in ([score()], [score(), score(song_id=9999, achievements=99)]):
            with (
                self.subTest(after=after),
                patch(
                    "backend.score_sync.httpx.get",
                    side_effect=upload_get(before=[extra], after=after),
                ),
                patch(
                    "backend.score_sync.httpx.post",
                    return_value=response({"message": "更新成功", "updates": 0, "creates": 1}),
                ) as post,
            ):
                jobs.start_upload()
                result = self.finish(jobs)
                self.assertEqual(result["error"]["code"], "upload_verification_mismatch")
                receipt = self.store.meta("last_divingfish_upload")
                self.assertEqual(receipt["verification_state"], "mismatch")
                self.assertEqual(receipt["mismatch_count"], 1)
                self.assertEqual(receipt["mismatches"][0]["song_id"], 9999)
                self.assertTrue(receipt["upload_confirmed"])
                post.assert_called_once()

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

    def test_maintenance_rejects_new_session_without_probe_or_state_change(self):
        self.save_claim()
        jobs = self.job(clock=lambda: DURING_MAINTENANCE)
        before = self.state()
        with (
            patch("backend.score_sync.wahlap_available") as probe,
            patch("backend.score_sync.fetch_wahlap_scores") as fetch,
        ):
            with self.assertRaises(ScoreSyncError) as failure:
                jobs.start_wahlap(TEST_QR)
            status = jobs.status()
        self.assertEqual(failure.exception.status, 503)
        self.assertEqual(failure.exception.code, "maintenance")
        self.assertEqual(failure.exception.retry_at, "2026-10-13T07:00:00+08:00")
        self.assertEqual(failure.exception.headers(), {"Retry-After": "5400"})
        self.assertTrue(status["wahlap"]["maintenance"]["active"])
        self.assertFalse(status["wahlap"]["available"])
        self.assertIsNone(status["job"])
        self.assertEqual(self.state(), before)
        self.assertNotIn(TEST_QR, json.dumps(status) + str(failure.exception.detail()))
        probe.assert_not_called()
        fetch.assert_not_called()
        with self.store.sync_lease():
            pass

    def test_worker_rechecks_schedule_after_request_before_fetch(self):
        clock = Mock(
            side_effect=[
                datetime.fromisoformat("2026-10-13T03:59:59+08:00"),
                datetime.fromisoformat("2026-10-13T04:00:00+08:00"),
                DURING_MAINTENANCE,
            ]
        )
        jobs = self.job(clock=clock)
        before = self.state()
        with patch("backend.score_sync.fetch_wahlap_scores") as fetch:
            jobs.start_wahlap(TEST_QR)
            result = self.finish(jobs)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "maintenance")
        self.assertEqual(result["error"]["retry_after_seconds"], 10800)
        self.assertEqual(result["error"]["retry_at"], "2026-10-13T07:00:00+08:00")
        self.assertEqual(self.state(), before)
        fetch.assert_not_called()
        with self.store.sync_lease():
            pass

    def test_inflight_session_can_finish_and_confirm_during_maintenance(self):
        self.save_claim()
        current = [datetime.fromisoformat("2026-10-13T03:59:59+08:00")]
        jobs = self.job(clock=lambda: current[0])
        started, release = threading.Event(), threading.Event()

        def fetch(_):
            started.set()
            self.assertTrue(release.wait(3))
            return payload()

        with patch("backend.score_sync.fetch_wahlap_scores", side_effect=fetch):
            jobs.start_wahlap(TEST_QR)
            self.assertTrue(started.wait(2))
            current[0] = DURING_MAINTENANCE
            with patch("backend.score_sync.wahlap_available") as probe:
                self.assertEqual(jobs.status()["job"]["state"], "running")
                with self.assertRaises(ScoreSyncError) as failure:
                    jobs.start_wahlap(TEST_QR)
                self.assertEqual(failure.exception.code, "maintenance")
                release.set()
                result = self.finish(jobs)
            probe.assert_not_called()
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(result["result"]["logout_confirmed"])
        self.assertEqual(result["result"]["confirmed"], 1)
        self.assertEqual(self.store.chart(self.key)["venue"], "测试机厅")

    def test_maintenance_does_not_block_explicit_divingfish_upload(self):
        self.import_game()
        self.store.save_token(TEST_TOKEN)
        jobs = self.job(clock=lambda: DURING_MAINTENANCE)
        with (
            patch("backend.score_sync.wahlap_available") as probe,
            patch("backend.score_sync.httpx.get", side_effect=upload_get()),
            patch(
                "backend.score_sync.httpx.post",
                return_value=response({"message": "更新成功", "updates": 1, "creates": 0}),
            ) as upload,
        ):
            jobs.start_upload()
            result = self.finish(jobs)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["uploaded_count"], 1)
        upload.assert_called_once()
        probe.assert_not_called()

    def test_maintenance_api_error_headers_and_other_local_actions_remain_available(self):
        with self.store.db() as conn:
            conn.execute("INSERT INTO venues VALUES(?,?,?)", ("测试机厅", "fixture", ""))
        with (
            patch("backend.score_sync.utc_now", return_value=DURING_MAINTENANCE),
            patch("backend.score_sync.wahlap_available") as probe,
            patch("backend.score_sync.fetch_wahlap_scores") as fetch,
            TestClient(create_app(self.store, updater=FakeUpdater())) as client,
        ):
            status = client.get("/api/sync/status")
            rejected = client.post("/api/sync/wahlap", json={"qr_text": TEST_QR})
            self.assertEqual(rejected.status_code, 503)
            self.assertEqual(rejected.headers["Retry-After"], "5400")
            self.assertEqual(rejected.json()["detail"]["code"], "maintenance")
            self.assertEqual(rejected.json()["detail"]["retry_at"], "2026-10-13T07:00:00+08:00")
            self.assertTrue(status.json()["wahlap"]["maintenance"]["active"])
            self.assertEqual(client.get("/api/charts").status_code, 200)
            saved = client.put(
                f"/api/charts/{self.key}/ap-record",
                json={"venue": "测试机厅", "request_id": "maintenance", "expected_revision": 0},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["chart"]["pending_record"]["venue"], "测试机厅")
            self.assertNotIn(TEST_QR, status.text + rejected.text)
        probe.assert_not_called()
        fetch.assert_not_called()

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
