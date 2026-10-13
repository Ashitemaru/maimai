import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from backend import wahlap, wahlap_service, wahlap_worker
from scripts import setup_wahlap_runtime

QR = "SGWCMAID261013120000TESTPAYLOAD"


def detail(**values):
    return {
        "musicId": 10001,
        "level": 3,
        "achievement": 1004565,
        "comboStatus": 3,
        "syncStatus": 5,
        "deluxscoreMax": 1200,
        **values,
    }


def page(*details, cursor=0, **values):
    return {
        "userMusicList": [{"userMusicDetailList": list(details)}],
        "nextIndex": cursor,
        **values,
    }


def result(**values):
    return {
        "records": [wahlap_worker.normalize_detail(detail())],
        "player_id": "123",
        "logout_confirmed": False,
        "logout_status": "unverified",
        **values,
    }


class Pages:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    async def post(self, api, player_id, payload):
        self.calls.append((api, player_id, payload))
        return next(self.pages)


class ScoreValidationTests(unittest.TestCase):
    def test_exact_mark_mapping_including_sync_and_zero(self):
        for combo, expected in enumerate(("", "fc", "fcp", "ap", "app")):
            self.assertEqual(
                wahlap_worker.normalize_detail(detail(comboStatus=combo))["fc"], expected
            )
        for sync, expected in enumerate(("", "fs", "fsp", "fsd", "fsdp", "sync")):
            self.assertEqual(
                wahlap_worker.normalize_detail(detail(syncStatus=sync))["fs"], expected
            )
        self.assertEqual(wahlap_worker.normalize_detail(detail())["achievements"], 100.4565)

    def test_no_achievement_inference_of_ap(self):
        record = wahlap_worker.normalize_detail(detail(achievement=1010000, comboStatus=0))
        self.assertEqual(record["fc"], "")

    def test_each_row_requires_both_marks_and_score_fields(self):
        for key in ("level", "achievement", "comboStatus", "syncStatus", "deluxscoreMax"):
            with self.subTest(key=key):
                value = detail()
                del value[key]
                with self.assertRaises(wahlap.WahlapError) as raised:
                    wahlap_worker.normalize_detail(value)
                self.assertEqual(raised.exception.code, "incomplete_scores")

    def test_invalid_ids_enums_and_numbers_are_not_coerced(self):
        for field, value in (
            ("musicId", 0),
            ("musicId", True),
            ("musicId", "10001"),
            ("level", -1),
            ("level", 6),
            ("achievement", -1),
            ("achievement", 1010001),
            ("achievement", float("nan")),
            ("comboStatus", None),
            ("comboStatus", 5),
            ("syncStatus", 6),
            ("syncStatus", "AP"),
            ("deluxscoreMax", -1),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(wahlap.WahlapError):
                wahlap_worker.normalize_detail(detail(**{field: value}))

    def test_utage_achievement_can_exceed_101(self):
        self.assertEqual(
            wahlap_worker.normalize_detail(detail(musicId=100001, achievement=1200000))[
                "achievements"
            ],
            120,
        )

    def test_outer_identity_and_dx_aliases_must_agree(self):
        value = detail()
        del value["musicId"]
        self.assertEqual(wahlap_worker.normalize_detail(value, 10001)["song_id"], 10001)
        with self.assertRaises(wahlap.WahlapError):
            wahlap_worker.normalize_detail(detail(), 10002)
        with self.assertRaises(wahlap.WahlapError):
            wahlap_worker.normalize_detail(detail(dxScore=1300))

    def test_child_boundary_revalidates_and_drops_unexpected_metadata(self):
        data = result(credentials="do not return", source="something else")
        data["records"][0]["raw_secret"] = "do not return"
        clean = wahlap.validate_result(data)
        self.assertNotIn("credentials", clean)
        self.assertNotIn("raw_secret", clean["records"][0])
        self.assertEqual(clean["source"], "wahlap")

    def test_child_boundary_rejects_duplicates_missing_marks_nan_and_false_confirmation(self):
        bad = []
        data = result()
        data["records"] *= 2
        bad.append(data)
        data = result()
        del data["records"][0]["fc"]
        bad.append(data)
        data = result()
        data["records"][0]["achievements"] = float("nan")
        bad.append(data)
        bad += [
            result(logout_confirmed=True),
            result(player_id=123),
            result(logout_status="arbitrary"),
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(wahlap.WahlapError):
                wahlap.validate_result(data)
        self.assertTrue(
            wahlap.validate_result(result(logout_status="confirmed", logout_confirmed=True))[
                "logout_confirmed"
            ]
        )


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_pages_preserve_original_ids_and_difficulty(self):
        client = Pages([page(detail(), cursor=50), page(detail(musicId=10002, level=4))])
        records = await wahlap_worker.read_all_scores(client, 123)
        self.assertEqual(
            [(r["song_id"], r["level_index"]) for r in records], [(10001, 3), (10002, 4)]
        )
        self.assertEqual([call[2]["nextIndex"] for call in client.calls], [0, 50])

    async def test_empty_account_requires_valid_explicit_terminal_page(self):
        self.assertEqual(
            await wahlap_worker.read_all_scores(
                Pages([{"userMusicList": [], "nextIndex": 0}]), 123
            ),
            [],
        )
        for payload in ({}, {"userMusicList": []}):
            with self.subTest(payload=payload), self.assertRaises(wahlap.WahlapError):
                await wahlap_worker.read_all_scores(Pages([payload]), 123)

    async def test_repeated_chart_or_cursor_and_wrong_player_rejected(self):
        scenarios = [
            [page(detail(), cursor=50), page(detail())],
            [page(detail(), cursor=50), page(detail(musicId=2), cursor=50)],
            [page(detail(), userId=999)],
            [page(detail(), cursor=-1)],
        ]
        for pages in scenarios:
            with self.subTest(pages=pages), self.assertRaises(wahlap.WahlapError):
                await wahlap_worker.read_all_scores(Pages(pages), 123)

    async def test_partial_flags_on_later_page_fail_the_whole_fetch(self):
        missing = detail(musicId=2)
        del missing["syncStatus"]
        with self.assertRaises(wahlap.WahlapError) as raised:
            await wahlap_worker.read_all_scores(
                Pages([page(detail(), cursor=50), page(missing)]), 123
            )
        self.assertEqual(raised.exception.code, "incomplete_scores")

    async def test_page_and_record_caps_terminate(self):
        with (
            patch.object(wahlap_worker, "MAX_PAGES", 1),
            self.assertRaises(wahlap.WahlapError) as raised,
        ):
            await wahlap_worker.read_all_scores(Pages([page(detail(), cursor=50)]), 123)
        self.assertEqual(raised.exception.pagination_reason, "page_limit")
        with (
            patch.object(wahlap_worker, "MAX_RECORDS", 1),
            self.assertRaises(wahlap.WahlapError) as raised,
        ):
            await wahlap_worker.read_all_scores(Pages([page(detail(), detail(musicId=2))]), 123)
        self.assertEqual(raised.exception.pagination_reason, "record_limit")

    async def test_wide_and_nonmonotonic_cursors_are_echoed_without_offset_arithmetic(self):
        wide = 50_000_000_050
        client = Pages(
            [
                page(detail(), cursor=wide),
                page(detail(musicId=2), cursor=100),
                page(detail(musicId=3), cursor=str(wahlap_worker.MAX_CURSOR)),
                page(detail(musicId=4), cursor="0"),
            ]
        )
        records = await wahlap_worker.read_all_scores(client, 123)
        self.assertEqual(len(records), 4)
        self.assertEqual(
            [call[2]["nextIndex"] for call in client.calls],
            [0, wide, 100, wahlap_worker.MAX_CURSOR],
        )

    async def test_empty_intermediate_page_is_not_mistaken_for_completion(self):
        client = Pages(
            [
                page(detail(), cursor=50_000_000_050),
                {"userMusicList": [], "nextIndex": "50"},
                page(detail(musicId=2), cursor="0"),
            ]
        )
        records = await wahlap_worker.read_all_scores(client, 123)
        self.assertEqual([record["song_id"] for record in records], [10001, 2])
        self.assertEqual(len(client.calls), 3)

    async def test_string_and_integer_aliases_cannot_bypass_cursor_cycle_detection(self):
        client = Pages(
            [
                {"userMusicList": [], "nextIndex": "50"},
                {"userMusicList": [], "nextIndex": 60},
                {"userMusicList": [], "nextIndex": 50},
            ]
        )
        with self.assertRaises(wahlap.WahlapError) as raised:
            await wahlap_worker.read_all_scores(client, 123)
        self.assertEqual(raised.exception.pagination_reason, "cursor_cycle")
        self.assertEqual(len(client.calls), 3)

    async def test_cursor_rejects_coercions_overflow_and_private_arbitrary_text(self):
        invalid = (
            None,
            True,
            False,
            1.0,
            -1,
            "-1",
            "00",
            "01",
            "+1",
            " 1",
            "1 ",
            "1.0",
            "1e3",
            "١",
            "１",
            QR,
            [],
            {},
            wahlap_worker.MAX_CURSOR + 1,
            str(wahlap_worker.MAX_CURSOR + 1),
            "1" * 1000,
        )
        for value in invalid:
            with (
                self.subTest(value_type=type(value).__name__),
                self.assertRaises(wahlap.WahlapError) as raised,
            ):
                await wahlap_worker.read_all_scores(Pages([page(detail(), cursor=value)]), 123)
            self.assertEqual(raised.exception.code, "pagination_invalid")
            self.assertIn(raised.exception.pagination_reason, {"cursor_type", "cursor_range"})
            self.assertNotIn(QR, str(raised.exception))

    async def test_oversized_page_remains_bounded(self):
        payload = {"userMusicList": [{}] * (wahlap_worker.PAGE_SIZE + 1), "nextIndex": 0}
        with self.assertRaises(wahlap.WahlapError) as raised:
            await wahlap_worker.read_all_scores(Pages([payload]), 123)
        self.assertEqual(raised.exception.pagination_reason, "group_limit")


class LifecycleClient:
    base_url = "https://maimai-gm.wahlap.com:42081/Maimai2Servlet/"

    def __init__(self):
        self.calls = []
        self.previews = iter([{"errorId": 0, "isLogin": False}] * 2)
        self.login = {"returnCode": 1, "_loginDateTime": 123456}
        self.music = page(detail())
        self.logout = {}
        self.configuration = {"gameSetting": {"movieServerUri": self.base_url}}

    async def get_game_setting(self, **kwargs):
        self.calls.append("configuration")
        if isinstance(self.configuration, Exception):
            raise self.configuration
        return self.configuration

    def with_base_url(self, url):
        other = LifecycleClient()
        other.base_url = url
        other.calls = self.calls
        return other

    async def get_user_preview(self, session):
        self.calls.append("preview")
        value = next(self.previews)
        if isinstance(value, Exception):
            raise value
        return value

    async def user_login(self, session, **kwargs):
        self.calls.append("login")
        if isinstance(self.login, Exception):
            raise self.login
        return self.login

    async def post(self, api, player_id, payload):
        self.calls.append(api)
        value = self.music if api == "GetUserMusicApi" else self.logout
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        self.calls.append("close")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.transport = LifecycleClient()
        transport = self.transport

        class Owner:
            region_id, place_id = 8, 3496
            client_id = cabinet_client_id = "fixture"

            def __init__(self, **kwargs):
                pass

            async def resolve_session_async(self, sgid):
                transport.calls.append("authorization")
                return SimpleNamespace(user_id=123, token="fixture-not-a-secret")

        self.modules = SimpleNamespace(
            client=SimpleNamespace(MaimaiOfficialClient=Owner),
            protocol=SimpleNamespace(
                DEFAULT_OFFICIAL_TITLE_ENDPOINTS=[
                    SimpleNamespace(base_url=transport.base_url, host_header="", verify_tls=True)
                ],
                DEFAULT_RUNTIME_SERVER_URI="/Maimai2Servlet/",
                OfficialTitleServerError=OfficialTitleError,
                OfficialTitleClient=lambda **kwargs: transport,
            ),
        )

    async def test_confirmed_only_after_post_logout_preview(self):
        output = await wahlap_worker.collect_scores(QR, self.modules)
        self.assertTrue(output["logout_confirmed"])
        self.assertEqual(
            self.transport.calls,
            [
                "configuration",
                "authorization",
                "preview",
                "login",
                "GetUserMusicApi",
                "UserLogoutApi",
                "preview",
                "close",
            ],
        )

    async def test_active_or_unknown_preview_never_logs_in_or_out(self):
        for preview, code in (
            ({"errorId": 0, "isLogin": True}, "session_active"),
            ({"errorId": 0, "isLogin": 1}, "session_active"),
            ({"errorId": 0}, "session_state_unknown"),
            ({"errorId": 0, "isLogin": "false"}, "session_state_unknown"),
            ({"errorId": 1, "isLogin": False}, "session_state_unknown"),
        ):
            with self.subTest(preview=preview):
                self.transport.previews = iter([preview])
                self.transport.calls = []
                with self.assertRaises(wahlap.WahlapError) as raised:
                    await wahlap_worker.collect_scores(QR, self.modules)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("login", self.transport.calls)
                self.assertNotIn("UserLogoutApi", self.transport.calls)

    async def test_rejected_or_ambiguous_login_does_not_force_logout(self):
        for response in ({"returnCode": 100}, {}, TimeoutError("private QR here")):
            with self.subTest(response=response):
                self.transport.previews = iter([{"errorId": 0, "isLogin": False}])
                self.transport.calls = []
                self.transport.login = response
                with self.assertRaises(wahlap.WahlapError):
                    await wahlap_worker.collect_scores(QR, self.modules)
                self.assertNotIn("GetUserMusicApi", self.transport.calls)
                self.assertNotIn("UserLogoutApi", self.transport.calls)

    async def test_failed_score_read_still_attempts_logout_and_checks_preview(self):
        self.transport.music = RuntimeError("private QR here")
        with self.assertRaises(wahlap.WahlapError) as raised:
            await wahlap_worker.collect_scores(QR, self.modules)
        self.assertNotIn("private", str(raised.exception))
        self.assertTrue(raised.exception.logout_confirmed)
        self.assertEqual(self.transport.calls[-3:], ["UserLogoutApi", "preview", "close"])

    async def test_score_failure_does_not_hide_logout_failure(self):
        self.transport.music = RuntimeError("score failure")
        self.transport.logout = RuntimeError("logout failure")
        with self.assertRaises(wahlap.WahlapError) as raised:
            await wahlap_worker.collect_scores(QR, self.modules)
        self.assertEqual(raised.exception.code, "worker_failed")
        self.assertEqual(raised.exception.stage, "scores")
        self.assertEqual(raised.exception.logout_status, "failed")
        self.assertFalse(raised.exception.logout_confirmed)

    async def test_http_success_does_not_prove_logout(self):
        for after in ({"errorId": 0, "isLogin": True}, {}, {"errorId": 1, "isLogin": False}):
            with self.subTest(after=after):
                self.transport.previews = iter([{"errorId": 0, "isLogin": False}, after])
                output = await wahlap_worker.collect_scores(QR, self.modules)
                self.assertFalse(output["logout_confirmed"])
                self.assertEqual(output["logout_status"], "unverified")
                self.assertEqual(len(output["records"]), 1)

    async def test_logout_request_failure_preserves_valid_scores_but_reports_failure(self):
        self.transport.logout = RuntimeError("contains credentials")
        output = await wahlap_worker.collect_scores(QR, self.modules)
        self.assertEqual(output["logout_status"], "failed")
        self.assertFalse(output["logout_confirmed"])
        self.assertNotIn("credentials", json.dumps(output))

    async def test_network_probe_only_reads_configuration(self):
        self.assertEqual(
            await wahlap_worker.probe_network(self.modules),
            {"ok": True, "probe": "network", "account_access": False},
        )
        self.assertEqual(self.transport.calls, ["configuration", "close"])

    async def test_preflight_errors_never_process_qr_or_access_account(self):
        cases = [
            (OfficialTitleError("empty official title response"), "upstream_empty_response"),
            (OfficialTitleError("invalid official title response"), "upstream_decode_error"),
            (TimeoutError(QR), "upstream_timeout"),
            (httpx.ConnectError(QR), "upstream_transport_error"),
            (RuntimeError(QR), "worker_failed"),
            ({}, "upstream_decode_error"),
            ({"gameSetting": {}}, "upstream_decode_error"),
            ({"gameSetting": "private"}, "upstream_decode_error"),
            ({"gameSetting": {"movieServerUri": 123}}, "upstream_decode_error"),
            (
                {"gameSetting": {"movieServerUri": "https://untrusted.invalid/" + QR}},
                "invalid_endpoint",
            ),
        ]
        for configuration, code in cases:
            with self.subTest(code=code):
                self.transport.configuration = configuration
                self.transport.calls = []
                with self.assertRaises(wahlap.WahlapError) as raised:
                    await wahlap_worker.collect_scores(QR, self.modules)
                error = raised.exception
                self.assertEqual(error.code, code)
                self.assertEqual(error.stage, "configuration")
                self.assertEqual(error.logout_status, "not_attempted")
                self.assertEqual(self.transport.calls, ["configuration", "close"])
                self.assertNotIn(QR, json.dumps(wahlap.error_response(error)))

    async def test_authorization_failure_closes_settings_and_runtime_clients(self):
        self.transport.configuration = {"gameSetting": {"movieServerUri": "/different/"}}

        async def fail_authorization(owner, sgid):
            self.transport.calls.append("authorization")
            raise RuntimeError(QR)

        with (
            patch.object(
                self.modules.client.MaimaiOfficialClient,
                "resolve_session_async",
                fail_authorization,
            ),
            self.assertRaises(wahlap.WahlapError) as raised,
        ):
            await wahlap_worker.collect_scores(QR, self.modules)
        self.assertEqual(raised.exception.code, "authorization_failed")
        self.assertEqual(raised.exception.stage, "authorization")
        self.assertEqual(self.transport.calls, ["configuration", "authorization", "close", "close"])

    async def test_known_http_exception_retains_only_status_and_stage(self):
        self.transport.configuration = httpx.HTTPStatusError(
            QR,
            request=httpx.Request("POST", "https://example.invalid/" + QR),
            response=httpx.Response(403, content=QR.encode()),
        )
        with self.assertRaises(wahlap.WahlapError) as raised:
            await wahlap_worker.probe_network(self.modules)
        error = raised.exception
        self.assertEqual(error.code, "upstream_http_error")
        self.assertEqual(error.http_status, 403)
        self.assertEqual(error.stage, "configuration")
        self.assertNotIn(QR, json.dumps(wahlap.error_response(error)))

    async def test_unknown_server_error_is_not_classified_using_partial_text(self):
        self.transport.configuration = OfficialTitleError("empty official title response " + QR)
        with self.assertRaises(wahlap.WahlapError) as raised:
            await wahlap_worker.probe_network(self.modules)
        self.assertEqual(raised.exception.code, "worker_failed")

    async def test_cleanup_error_does_not_erase_primary_error(self):
        self.transport.configuration = OfficialTitleError("empty official title response")

        async def fail_close():
            raise RuntimeError(QR)

        with (
            patch.object(self.transport, "close", fail_close),
            self.assertRaises(wahlap.WahlapError) as raised,
        ):
            await wahlap_worker.probe_network(self.modules)
        self.assertEqual(raised.exception.code, "upstream_empty_response")


class OfficialTitleError(RuntimeError):
    pass


class ProcessBoundaryTests(unittest.TestCase):
    def test_sandbox_failure_is_unavailable_not_a_direct_execution_fallback(self):
        paths = (Path("/opt/runtime"), Path("/srv/state"), "/usr/bin/wine", "/usr/bin/bwrap")
        wahlap._sandbox_available.cache_clear()
        with (
            patch.object(wahlap, "_configured_paths", return_value=paths),
            patch.object(
                wahlap, "_run_worker", side_effect=wahlap.WahlapError("worker_failed")
            ) as run,
        ):
            self.assertFalse(wahlap._local_available()["available"])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], "/usr/bin/bwrap")
        wahlap._sandbox_available.cache_clear()

    def test_input_validation_never_reflects_credential(self):
        self.assertEqual(wahlap.validate_sgid(f" {QR} "), QR)
        for value in (None, "not-a-qr", QR + "\ncredential", "https://example.com/" + QR, QR * 100):
            with self.subTest(value=type(value)), self.assertRaises(wahlap.WahlapError) as raised:
                wahlap.validate_sgid(value)
            self.assertNotIn(QR, str(raised.exception))

    def test_subprocess_uses_stdin_and_drops_arbitrary_error_text(self):
        code = "import sys,json; value=json.load(sys.stdin); print(json.dumps({'ok':False,'error':value['sgid']}))"
        with self.assertRaises(wahlap.WahlapError) as raised:
            wahlap._run_worker([sys.executable, "-c", code], {"sgid": QR}, timeout=5)
        self.assertEqual(raised.exception.code, "worker_failed")
        self.assertNotIn(QR, str(raised.exception))

    def test_child_environment_does_not_inherit_application_secrets(self):
        code = "import os,json; print(json.dumps({'ok':True,'leaked':'MAIMAI_MASTER_KEY' in os.environ}))"
        with patch.dict(os.environ, {"MAIMAI_MASTER_KEY": "fake-sensitive"}):
            output = wahlap._run_worker([sys.executable, "-c", code], {}, timeout=5)
        self.assertFalse(output["leaked"])

    def test_timeout_and_output_limit_stop_worker(self):
        with self.assertRaises(wahlap.WahlapError) as raised:
            wahlap._run_worker(
                [sys.executable, "-c", "import time; time.sleep(30)"], {}, timeout=0.1
            )
        self.assertEqual(raised.exception.code, "worker_timeout")
        with (
            patch.object(wahlap, "MAX_OUTPUT_BYTES", 100),
            self.assertRaises(wahlap.WahlapError) as raised,
        ):
            wahlap._run_worker([sys.executable, "-c", "print('X'*1000)"], {}, timeout=5)
        self.assertEqual(raised.exception.code, "invalid_response")

    def test_failed_response_keeps_safe_logout_status(self):
        payload = {
            "ok": False,
            "error": "invalid_scores",
            "logout_status": "failed",
            "logout_confirmed": False,
        }
        code = f"print({json.dumps(payload)!r})"
        with self.assertRaises(wahlap.WahlapError) as raised:
            wahlap._run_worker([sys.executable, "-c", code], {}, timeout=5)
        self.assertEqual(raised.exception.logout_status, "failed")
        self.assertFalse(raised.exception.logout_confirmed)

    def test_error_diagnostics_are_allowlisted_at_child_boundary(self):
        for stage, status, expected_stage, expected_status in (
            ("configuration", 200, "configuration", 200),
            ("scores", 503, "scores", 503),
            (QR, QR, None, None),
            ({"value": QR}, True, None, None),
            ("configuration", 99, "configuration", None),
            ("configuration", 600, "configuration", None),
        ):
            with self.subTest(stage=expected_stage, status=expected_status):
                error = wahlap._response_error(
                    {
                        "error": "upstream_http_error",
                        "stage": stage,
                        "http_status": status,
                        "raw": QR,
                    }
                )
                self.assertEqual(error.stage, expected_stage)
                self.assertEqual(error.http_status, expected_status)
                self.assertNotIn(QR, json.dumps(wahlap.error_response(error)))

    def test_pagination_diagnostics_roundtrip_allowlist_never_reflects_arbitrary_values(self):
        for reason in (
            "cursor_type",
            "cursor_range",
            "cursor_cycle",
            "group_limit",
            "record_limit",
            "page_limit",
            QR,
            {"private": QR},
            None,
        ):
            payload = {
                "error": "pagination_invalid",
                "stage": "scores",
                "pagination_reason": reason,
                "logout_status": "confirmed",
                "logout_confirmed": True,
            }
            error = wahlap._response_error(payload)
            reply = wahlap.error_response(error)
            self.assertEqual(
                reply.get("pagination_reason"),
                reason if isinstance(reason, str) and reason in wahlap.PAGINATION_REASONS else None,
            )
            self.assertTrue(reply["logout_confirmed"])
            self.assertEqual(reply["stage"], "scores")
            self.assertNotIn(QR, json.dumps(reply))
        wrong_code = wahlap._response_error(
            {"error": "worker_failed", "pagination_reason": "cursor_range"}
        )
        self.assertNotIn("pagination_reason", wahlap.error_response(wrong_code))

    def test_contradictory_logout_error_receipt_is_never_confirmed(self):
        for flag in (False, None, "true", 1):
            error = wahlap._response_error(
                {
                    "error": "pagination_invalid",
                    "pagination_reason": "cursor_range",
                    "logout_status": "confirmed",
                    "logout_confirmed": flag,
                }
            )
            self.assertFalse(error.logout_confirmed)
            self.assertEqual(error.logout_status, "unverified")

    def test_network_probe_uses_sandbox_and_exclusive_worker_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            paths = (Path("/opt/runtime"), state, "/usr/bin/wine", "/usr/bin/bwrap")
            with (
                patch.object(wahlap, "_configured_paths", return_value=paths),
                patch.object(
                    wahlap,
                    "_run_worker",
                    return_value={"probe": "network", "account_access": False},
                ) as run,
            ):
                self.assertEqual(wahlap._probe_network_local()["probe"], "network")
                self.assertEqual(run.call_args.args[0][0], "/usr/bin/bwrap")
                self.assertEqual(run.call_args.args[0][-1], "--probe-network")
                self.assertEqual(run.call_args.args[1], {})
                self.assertEqual(run.call_args.kwargs["timeout"], 30)
                with wahlap._single_worker(state), self.assertRaises(wahlap.WahlapError) as raised:
                    wahlap._probe_network_local()
                self.assertEqual(raised.exception.code, "busy")
                self.assertEqual(run.call_count, 1)

    def test_single_worker_lock_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as directory, wahlap._single_worker(Path(directory)):
            with self.assertRaises(wahlap.WahlapError) as raised:
                with wahlap._single_worker(Path(directory)):
                    self.fail("second process lock must not succeed")
            self.assertEqual(raised.exception.code, "busy")

    def test_sandbox_only_binds_dedicated_state_and_readonly_sources(self):
        command = wahlap._sandbox_command(
            Path("/opt/runtime"), Path("/srv/private-state"), "/usr/bin/wine", "/usr/bin/bwrap"
        )
        binds = [command[i + 1 : i + 3] for i, item in enumerate(command) if item == "--bind"]
        self.assertEqual(binds, [["/srv/private-state", "/state"]])
        self.assertIn("--unshare-all", command)
        self.assertIn("--unshare-user", command)
        self.assertIn("--disable-userns", command)
        self.assertIn("--clearenv", command)
        self.assertNotIn("/srv/mai-journal/shared/runtime", command)
        self.assertNotIn("/home", command)
        self.assertNotIn("/root", command)

    def test_installation_hashes_are_checked_before_any_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "upstream/helper.py"
            file.parent.mkdir()
            file.write_text("safe fixture")
            lock = {
                "commit": wahlap.UPSTREAM_COMMIT,
                "files": {"helper.py": hashlib.sha256(file.read_bytes()).hexdigest()},
            }
            lock_path = root / "lock.json"
            lock_path.write_text(json.dumps(lock))
            (root / "runtime.json").write_text(json.dumps(lock))
            with patch.object(wahlap, "LOCK_PATH", lock_path):
                self.assertEqual(wahlap.verify_runtime(root), lock)
                file.write_text("changed")
                with self.assertRaises(wahlap.WahlapError) as raised:
                    wahlap.verify_runtime(root)
                self.assertEqual(raised.exception.code, "runtime_integrity")

    def test_disallowed_runtime_url_rejected(self):
        for url in (
            "http://maimai-gm.wahlap.com:42081/a",
            "https://elsewhere.com:42081/a",
            "https://maimai-gm.wahlap.com/a",
        ):
            with self.subTest(url=url), self.assertRaises(wahlap.WahlapError):
                wahlap_worker.validate_endpoint(url)


class SocketBrokerTests(unittest.TestCase):
    def exchange(self, payload):
        destination = io.BytesIO()
        source = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        wahlap_service.serve(io.BytesIO(source), destination)
        return json.loads(destination.getvalue())

    def test_broker_probe_never_fetches_or_needs_credentials(self):
        with (
            patch.object(
                wahlap, "_local_available", return_value={"available": True, "reason": None}
            ),
            patch.object(wahlap, "_fetch_scores_local") as fetch,
        ):
            self.assertEqual(
                self.exchange({"kind": "probe"}), {"ok": True, "available": True, "reason": None}
            )
        fetch.assert_not_called()

    def test_broker_fetch_uses_local_boundary_without_socket_recursion(self):
        with (
            patch.object(wahlap, "_fetch_scores_local", return_value=result()) as local,
            patch.object(wahlap, "fetch_scores") as remote,
        ):
            response = self.exchange({"kind": "fetch", "sgid": QR})
        local.assert_called_once_with(QR)
        remote.assert_not_called()
        self.assertTrue(response["ok"])
        self.assertEqual(response["logout_status"], "unverified")

    def test_broker_network_probe_never_fetches_and_rejects_credentials(self):
        clean = {"ok": True, "probe": "network", "account_access": False}
        with (
            patch.object(wahlap, "_probe_network_local", return_value=clean) as probe,
            patch.object(wahlap, "_fetch_scores_local") as fetch,
        ):
            self.assertEqual(self.exchange({"kind": "network_probe"}), clean)
            self.assertEqual(
                self.exchange({"kind": "network_probe", "sgid": QR})["error"], "invalid_response"
            )
        probe.assert_called_once_with()
        fetch.assert_not_called()

    def test_network_probe_uncached_response_drops_extra_metadata(self):
        with (
            patch.dict(os.environ, {"MAIMAI_WAHLAP_ENABLED": "1"}),
            patch.object(
                wahlap,
                "_socket_request",
                return_value={"ok": True, "probe": "network", "account_access": False, "raw": QR},
            ) as request,
        ):
            self.assertEqual(
                wahlap.probe_network(), {"ok": True, "probe": "network", "account_access": False}
            )
            wahlap.probe_network()
            self.assertEqual(request.call_count, 2)
            request.assert_called_with({"kind": "network_probe"}, timeout=35)

    def test_network_probe_does_not_accept_imports_only_or_account_access_receipt(self):
        for reply in (
            {"ok": True, "probe": "imports", "account_access": False},
            {"ok": True, "probe": "network", "account_access": True},
            {"ok": True, "probe": "network", "account_access": 0},
        ):
            with (
                patch.dict(os.environ, {"MAIMAI_WAHLAP_ENABLED": "1"}),
                patch.object(wahlap, "_socket_request", return_value=reply),
                self.assertRaises(wahlap.WahlapError) as raised,
            ):
                wahlap.probe_network()
            self.assertEqual(raised.exception.code, "invalid_response")

    def test_network_probe_timeout_does_not_claim_uncertain_game_session(self):
        with (
            patch.dict(os.environ, {"MAIMAI_WAHLAP_ENABLED": "1"}),
            patch.object(
                wahlap, "_socket_request", side_effect=wahlap.WahlapError("worker_timeout")
            ),
            self.assertRaises(wahlap.WahlapError) as raised,
        ):
            wahlap.probe_network()
        self.assertEqual(raised.exception.code, "upstream_timeout")
        self.assertEqual(raised.exception.stage, "configuration")
        self.assertEqual(raised.exception.logout_status, "not_attempted")
        self.assertNotIn("会话", str(raised.exception))

    def test_broker_preserves_safe_failure_stage_and_status(self):
        error = wahlap.WahlapError("upstream_http_error", stage="configuration", http_status=403)
        with patch.object(wahlap, "_probe_network_local", side_effect=error):
            reply = self.exchange({"kind": "network_probe"})
        self.assertEqual(reply["stage"], "configuration")
        self.assertEqual(reply["http_status"], 403)
        self.assertEqual(reply["error"], "upstream_http_error")

    def test_broker_preserves_pagination_reason_and_post_logout_evidence(self):
        error = wahlap.WahlapError(
            "pagination_invalid", stage="scores", pagination_reason="cursor_range"
        )
        error.logout_status, error.logout_confirmed = "confirmed", True
        with patch.object(wahlap, "_fetch_scores_local", side_effect=error):
            reply = self.exchange({"kind": "fetch", "sgid": QR})
        self.assertEqual(reply["pagination_reason"], "cursor_range")
        self.assertEqual(reply["logout_status"], "confirmed")
        self.assertTrue(reply["logout_confirmed"])
        self.assertNotIn(QR, json.dumps(reply))

    def test_broker_rejects_oversized_invalid_and_extra_request_fields(self):
        with patch.object(wahlap, "_fetch_scores_local") as fetch:
            for request in (
                b"x" * 4097,
                b"{",
                [],
                {"kind": "fetch"},
                {"kind": "probe", "sgid": QR},
            ):
                with self.subTest(request=str(request)[:40]):
                    self.assertEqual(self.exchange(request)["error"], "invalid_response")
        fetch.assert_not_called()

    def test_broker_errors_never_echo_qr_or_arbitrary_exception_text(self):
        with patch.object(wahlap, "_fetch_scores_local", side_effect=RuntimeError(QR)):
            self.assertEqual(
                self.exchange({"kind": "fetch", "sgid": QR}),
                {"ok": False, "error": "worker_failed"},
            )
        error = wahlap.WahlapError("invalid_scores")
        error.logout_status, error.logout_confirmed = "confirmed", True
        with patch.object(wahlap, "_fetch_scores_local", side_effect=error):
            response = self.exchange({"kind": "fetch", "sgid": QR})
        self.assertEqual(response["logout_status"], "confirmed")
        self.assertTrue(response["logout_confirmed"])
        self.assertNotIn(QR, json.dumps(response))

    def connection(self, reply):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.side_effect = [json.dumps(reply).encode(), b""]
        return connection

    def test_web_adapter_uses_private_socket_with_qr_only_in_request_body(self):
        connection = self.connection({"ok": True, **result()})
        with (
            patch.dict(
                os.environ,
                {"MAIMAI_WAHLAP_ENABLED": "1", "MAIMAI_WAHLAP_SOCKET": "/run/private.sock"},
            ),
            patch.object(wahlap.socket, "socket", return_value=connection),
            patch.object(wahlap, "_fetch_scores_local") as local,
            patch.object(wahlap.subprocess, "Popen") as subprocess,
        ):
            output = wahlap.fetch_scores(QR)
        local.assert_not_called()
        subprocess.assert_not_called()
        connection.connect.assert_called_once_with("/run/private.sock")
        self.assertEqual(
            json.loads(connection.sendall.call_args.args[0]), {"kind": "fetch", "sgid": QR}
        )
        self.assertNotIn(QR, json.dumps(output))
        self.assertEqual(output["source"], "wahlap")

    def test_socket_response_is_bounded_and_errors_are_sanitized(self):
        for reply, limit, code in (
            ({"ok": False, "error": QR}, 10000, "worker_failed"),
            ({"ok": True, "large": "x" * 200}, 100, "invalid_response"),
        ):
            with (
                self.subTest(code=code),
                patch.object(wahlap.socket, "socket", return_value=self.connection(reply)),
                patch.object(wahlap, "MAX_OUTPUT_BYTES", limit),
                self.assertRaises(wahlap.WahlapError) as raised,
            ):
                wahlap._socket_request({"kind": "probe"})
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn(QR, str(raised.exception))

    def test_socket_timeout_unavailable_and_disabled_never_leak_input(self):
        for failure, code in (
            (TimeoutError(QR), "worker_timeout"),
            (OSError(QR), "service_unavailable"),
        ):
            connection = self.connection({})
            connection.connect.side_effect = failure
            with (
                patch.object(wahlap.socket, "socket", return_value=connection),
                self.assertRaises(wahlap.WahlapError) as raised,
            ):
                wahlap._socket_request({"kind": "fetch", "sgid": QR})
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn(QR, str(raised.exception))
        with (
            patch.dict(os.environ, {"MAIMAI_WAHLAP_ENABLED": "0"}),
            patch.object(wahlap, "_socket_request") as request,
        ):
            self.assertFalse(wahlap.available()["available"])
            with self.assertRaises(wahlap.WahlapError):
                wahlap.fetch_scores(QR)
        request.assert_not_called()

    def test_service_available_accepts_only_known_messages(self):
        wahlap._service_available.cache_clear()
        with patch.object(
            wahlap, "_socket_request", return_value={"ok": True, "available": False, "reason": QR}
        ):
            response = wahlap._service_available("fixture", 1)
        self.assertFalse(response["available"])
        self.assertEqual(response["reason"], wahlap.ERROR_MESSAGES["invalid_response"])
        wahlap._service_available.cache_clear()

    def test_service_is_private_and_does_not_relax_main_app(self):
        deployment = Path(__file__).resolve().parents[1] / "deploy"
        socket_unit = (deployment / "mai-journal-wahlap.socket").read_text()
        service_unit = (deployment / "mai-journal-wahlap@.service").read_text()
        app_unit = (deployment / "mai-journal.service").read_text()
        self.assertIn("SocketMode=0600", socket_unit)
        self.assertIn("Accept=yes", socket_unit)
        self.assertIn("ProtectKernelTunables=yes", app_unit)
        self.assertIn("ProtectKernelTunables=no", service_unit)
        self.assertNotIn("EnvironmentFile=", service_unit)
        self.assertIn(
            "/srv/mai-journal/current/venv/bin/python -m backend.wahlap_service", service_unit
        )


class RuntimeSetupTests(unittest.TestCase):
    def test_download_is_hash_checked_and_reuses_verified_files_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, runtime = root / "repo", root / "runtime"
            (repo / "deploy").mkdir(parents=True)
            body = b"fixture, never imported or executed"
            lock = {
                "commit": wahlap.UPSTREAM_COMMIT,
                "files": {"fixture.dat": hashlib.sha256(body).hexdigest()},
            }
            (repo / "deploy/wahlap-runtime.lock.json").write_text(json.dumps(lock))
            with (
                patch.object(setup_wahlap_runtime, "REPO", repo),
                patch.object(
                    setup_wahlap_runtime.urllib.request, "urlopen", return_value=io.BytesIO(body)
                ) as fetch,
                patch.object(setup_wahlap_runtime.subprocess, "run") as run,
                patch("builtins.print"),
            ):
                setup_wahlap_runtime.install(runtime, download_only=True)
                setup_wahlap_runtime.install(runtime, download_only=True)
            self.assertEqual(fetch.call_count, 1)
            run.assert_not_called()
            self.assertEqual(json.loads((runtime / "runtime.json").read_text()), lock)

    def test_corrupt_download_creates_no_runtime_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, runtime = root / "repo", root / "runtime"
            (repo / "deploy").mkdir(parents=True)
            lock = {"commit": wahlap.UPSTREAM_COMMIT, "files": {"fixture.dat": "0" * 64}}
            (repo / "deploy/wahlap-runtime.lock.json").write_text(json.dumps(lock))
            with (
                patch.object(setup_wahlap_runtime, "REPO", repo),
                patch.object(
                    setup_wahlap_runtime.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(b"tampered"),
                ),
                self.assertRaises(ValueError),
            ):
                setup_wahlap_runtime.install(runtime, download_only=True)
            self.assertFalse((runtime / "runtime.json").exists())

    def test_setup_rejects_runtime_in_source_tree(self):
        with self.assertRaises(ValueError):
            setup_wahlap_runtime.install(
                setup_wahlap_runtime.REPO / "private-runtime", download_only=True
            )


if __name__ == "__main__":
    unittest.main()
