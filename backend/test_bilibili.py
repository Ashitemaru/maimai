from __future__ import annotations

import base64
import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend.bilibili import (
    ApplyInput,
    BilibiliService,
    PrepareInput,
    bilibili_router,
    title_for,
    validate_png,
)
from backend.bilibili_client import (
    API,
    MEMBER,
    PASSPORT,
    BilibiliClient,
    BilibiliError,
    cover_identity,
    edit_payload,
)

BV = "BV1TbYo6xECK"
ACCOUNT = {"mid": 1234, "name": "Test"}


def archive():
    return {
        "archive": {
            "aid": 456,
            "mid": 1234,
            "bvid": BV,
            "title": "Original",
            "copyright": 1,
            "tid": 136,
            "tag": "music,test",
            "desc": "Keep this description",
            "cover": "https://i1.hdslb.com/bfs/archive/old.png",
            "cover43": "https://i1.hdslb.com/bfs/archive/old43.png",
            "dynamic": "unchanged",
            "no_reprint": 1,
            "is_dolby": 1,
            "lossless_music": 1,
            "human_type2": {"id": 1030, "name": "text"},
            "creation_statement": {"id": -1, "content": ""},
            "recreate": {"switch": -1, "auth": 1},
            "is_ab_cover": 0,
            "ab_cover_info": {"mode": 0, "ab_covers": None},
            "desc_v2": [{"type": 2, "raw_text": "mention", "biz_id": "5678"}],
        },
        "videos": [
            {
                "filename": "original-video",
                "title": "Original part title",
                "desc": "part desc",
                "cid": 789,
            }
        ],
        "space_hidden": 2,
        "watermark": {"state": 0, "mid": 1234},
        "subtitle": {"allow": False, "lan": ""},
        "in_season": False,
    }


def png(width, color="red"):
    out = io.BytesIO()
    with Image.new("RGB", (width, 1080), color) as image:
        image.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


class FakeStore:
    def __init__(self, path):
        self.runtime = Path(path)
        self.db_path = self.runtime / "personal.sqlite3"
        self.chart_value = {
            "chart_key": "123:3",
            "difficulty_index": 3,
            "title": "Example",
            "display_title": "Example",
            "type": "standard",
            "constant": 13.9,
            "level": "13+",
            "jp_release_version": "UNiVERSE",
        }

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def chart(self, key):
        return copy.deepcopy(self.chart_value) if key == self.chart_value["chart_key"] else None

    def all_charts(self):
        return [copy.deepcopy(self.chart_value), *copy.deepcopy(getattr(self, "extra_charts", []))]


class FakeClient:
    def __init__(self):
        self.data = archive()
        self.mid = 1234
        self.calls = []
        self.fail = None
        self.collection_members = {10: [], 20: []}
        self.edit_payloads = []
        self.delay_edit = False
        self.login_code = 0

    def __call__(self, *args, **kwargs):
        return self

    def close(self):
        pass

    def account(self):
        return {"mid": self.mid, "name": "Test"}

    def cookies(self):
        return [{"name": "SESSDATA", "value": "synthetic-session", "domain": ".bilibili.com"}]

    def csrf(self):
        return "synthetic-csrf"

    def archive(self, bvid, mid):
        if mid != self.data["archive"]["mid"]:
            raise BilibiliError("owner mismatch", status=409)
        return copy.deepcopy(self.data)

    def collections(self):
        return {
            "items": [
                {
                    "id": 1,
                    "title": "Test collection",
                    "sections": [{"id": 10, "title": "13.9"}, {"id": 20, "title": "14.0"}],
                }
            ]
        }

    def membership(self, data, sections):
        for key, episodes in self.collection_members.items():
            for episode in episodes:
                if episode["aid"] == 456:
                    return {"section_id": key, "episode_id": episode["id"]}
        return None

    def section(self, section_id):
        episodes = []
        for episode in self.collection_members[section_id]:
            defaults = {"cid": 789, "bvid": BV, "title": "【maimai】Example Master13.9 AP"}
            episodes.append(defaults | copy.deepcopy(episode))
        return {
            "section": {"id": section_id, "seasonId": 1, "type": 0, "title": str(section_id)},
            "episodes": episodes,
        }

    def videos(self, page):
        return {
            "items": [
                {"bvid": BV, "title": self.data["archive"]["title"]},
                *getattr(self, "extra_videos", []),
            ],
            "page": 1,
            "has_more": False,
        }

    def sort_section(self, header, ids):
        self.calls.append("sort")
        rows = {episode["id"]: episode for episode in self.collection_members[header["id"]]}
        if self.fail != "sort_unapplied":
            self.collection_members[header["id"]] = [rows[key] for key in ids]
        if self.fail in ("sort", "sort_unapplied"):
            raise BilibiliError("sort timed out", uncertain=True)

    def upload_cover(self, value):
        self.calls.append("cover")
        if self.fail == "cover":
            raise BilibiliError("upload failure", uncertain=True)
        return f"https://archive.biliimg.com/bfs/archive/new{len(self.calls)}.png"

    def edit(self, body):
        self.calls.append("edit")
        self.edit_payloads.append(copy.deepcopy(body))
        if not self.delay_edit:
            for key in ("cover", "cover43", "title"):
                self.data["archive"][key] = body[key]
        if self.fail == "edit":
            raise BilibiliError("edit timed out", uncertain=True)

    def remove_episode(self, episode_id):
        self.calls.append("remove")
        if self.fail == "remove":
            raise BilibiliError("remove timed out", uncertain=True)
        for key in self.collection_members:
            self.collection_members[key] = []
        self.data["in_season"] = False

    def add_episode(self, section_id, data):
        self.calls.append("add")
        if self.fail == "add":
            raise BilibiliError("add timed out", uncertain=True)
        self.collection_members[section_id].append({"aid": 456, "cid": 789, "id": 222})
        self.data["in_season"] = True

    def start_login(self):
        return {
            "qrcode_key": "synthetic-secret-key",
            "url": (
                "https://account.bilibili.com/h5/account-h5/auth/scan-web"
                "?navhide=1&callback=&qrcode_key=synthetic-secret-key"
            ),
        }

    def poll_login(self, key):
        return {"code": self.login_code}


def preparation(**changes):
    return PrepareInput(
        bvid=BV,
        chart_key="123:3",
        achievement="100.4567",
        combo="ap",
        constant="13.9",
        section_id=10,
        **changes,
    )


class BilibiliServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FakeStore(self.temp.name)
        self.client = FakeClient()
        self.service = BilibiliService(self.store, client_factory=self.client)
        self.service._save_session(ACCOUNT, self.client)
        self.addCleanup(self.service.close)
        self.cover16 = png(1920)
        self.cover43 = png(1440)

    def prepare(self):
        return self.service.prepare(preparation())

    def apply(self, plan):
        return self.service.apply(
            ApplyInput(plan_id=plan["plan_id"], cover16=self.cover16, cover43=self.cover43)
        )

    def test_prepare_freezes_metadata_without_writes_and_uses_explicit_score(self):
        plan = self.prepare()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(plan["title"], "【maimai】Example Master13.9 AP")
        self.assertEqual(plan["achievement"], "100.4567")
        self.store.chart_value["title"] = "Changed after preview"
        stored, _ = self.service._plan(plan["plan_id"])
        self.assertEqual(stored["chart"]["title"], "Example")
        self.assertNotIn("_archive", plan)

    def test_both_covers_metadata_collection_exact_once_and_cleanup(self):
        plan = self.prepare()
        result = self.apply(plan)
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(self.client.calls, ["cover", "cover", "edit", "add"])
        posted = self.client.edit_payloads[0]
        self.assertEqual(posted["videos"], archive()["videos"])
        self.assertEqual(posted["desc"], archive()["archive"]["desc"])
        self.assertEqual(posted["watermark"], {"state": 0})
        self.assertEqual(posted["human_type2"], 1030)
        self.assertEqual(posted["desc_v2"], archive()["archive"]["desc_v2"])
        self.assertEqual(self.apply(plan)["state"], "complete")
        self.assertEqual(len(self.client.calls), 4)
        with self.store.db() as db:
            row = db.execute("SELECT cover16,cover43 FROM bilibili_jobs").fetchone()
            self.assertEqual(tuple(row), (None, None))

    def test_stale_archive_refuses_all_writes(self):
        plan = self.prepare()
        self.client.data["archive"]["desc"] = "Modified outside"
        self.assertEqual(self.apply(plan)["state"], "failed")
        self.assertEqual(self.client.calls, [])

    def test_different_account_refused(self):
        plan = self.prepare()
        self.client.mid = 111
        with self.assertRaises(BilibiliError):
            self.apply(plan)
        self.assertEqual(self.client.calls, [])

    def test_uncertain_edit_readback_resumes_without_second_edit(self):
        plan = self.prepare()
        self.client.fail = "edit"
        result = self.apply(plan)
        self.assertEqual(result["state"], "uncertain")
        self.client.fail = None
        result = self.service.retry(result["job_id"])
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(self.client.calls.count("edit"), 1)
        self.assertEqual(self.client.calls.count("cover"), 2)

    def test_uncertain_edit_not_visible_never_resends(self):
        plan = self.prepare()
        self.client.fail = "edit"
        self.client.delay_edit = True
        result = self.apply(plan)
        self.client.fail = None
        result = self.service.retry(result["job_id"])
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(self.client.calls.count("edit"), 1)
        second = self.prepare()
        with self.assertRaises(BilibiliError):
            self.apply(second)

    def test_collection_timeout_never_blindly_repeats_add(self):
        plan = self.prepare()
        self.client.fail = "add"
        result = self.apply(plan)
        self.client.fail = None
        self.assertEqual(self.service.retry(result["job_id"])["state"], "uncertain")
        self.assertEqual(self.client.calls.count("add"), 1)
        self.client.collection_members[10] = [{"id": 333, "aid": 456}]
        self.assertEqual(self.service.retry(result["job_id"])["state"], "complete")

    def test_move_collection_remove_then_add(self):
        self.client.collection_members[20] = [{"id": 90, "aid": 456}]
        self.client.data["in_season"] = True
        result = self.apply(self.prepare())
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(self.client.calls[-2:], ["remove", "add"])

    def test_uncertain_remove_does_not_repeat_or_add(self):
        self.client.collection_members[20] = [{"id": 90, "aid": 456}]
        self.client.data["in_season"] = True
        plan = self.prepare()
        self.client.fail = "remove"
        result = self.apply(plan)
        self.client.fail = None
        result = self.service.retry(result["job_id"])
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(self.client.calls.count("remove"), 1)
        self.assertNotIn("add", self.client.calls)

    def test_already_member_does_not_add(self):
        self.client.collection_members[10] = [{"id": 90, "aid": 456}]
        self.client.data["in_season"] = True
        self.assertEqual(self.apply(self.prepare())["state"], "complete")
        self.assertNotIn("add", self.client.calls)

    def test_retry_preserves_images_and_cross_restart_state(self):
        self.client.fail = "add"
        result = self.apply(self.prepare())
        self.service.close()
        second = BilibiliService(self.store, client_factory=self.client)
        self.addCleanup(second.close)
        saved = second.job(result["job_id"])
        self.assertEqual(saved["covers"], {"cover16": self.cover16, "cover43": self.cover43})
        self.assertEqual(saved["plan"]["achievement"], "100.4567")
        self.client.collection_members[10] = [{"id": 90, "aid": 456}]
        self.assertEqual(second.retry(result["job_id"])["state"], "complete")

    def test_cover_bytes_cannot_change_during_retry(self):
        self.client.fail = "add"
        plan = self.prepare()
        self.apply(plan)
        self.cover16 = png(1920, "blue")
        with self.assertRaises(BilibiliError):
            self.apply(plan)

    def test_login_opaque_id_expiry_and_session_permissions(self):
        result = self.service.start_login()
        self.assertEqual(set(result), {"login_id", "url", "expires_at"})
        self.assertNotIn("synthetic-secret-key", result["login_id"])
        self.assertEqual(result["url"], self.client.start_login()["url"])
        answer = self.service.poll_login(result["login_id"])
        self.assertEqual(answer["state"], "connected")
        self.assertEqual(os.stat(self.service.session_path).st_mode & 0o777, 0o600)
        self.assertNotIn("synthetic-session", json.dumps(self.service.status()))
        self.service.disconnect()
        self.assertFalse(self.service.status()["connected"])
        self.assertEqual(self.service.poll_login(result["login_id"]), {"state": "expired"})

    def test_session_lock_rejects_concurrent_mutation(self):
        with self.service.operation(), self.assertRaises(BilibiliError):
            self.service.start_login()

    def test_ap_plus_perfect_score_allowed(self):
        body = preparation().model_copy(update={"achievement": "101.0000", "combo": "app"})
        self.assertTrue(self.service.prepare(body)["title"].endswith(" AP+"))

    def test_restart_marks_interrupted_running_job_without_losing_step(self):
        self.client.fail = "edit"
        result = self.apply(self.prepare())
        with self.store.db() as db:
            row = db.execute("SELECT payload FROM bilibili_jobs").fetchone()
            job = json.loads(row[0])
            job["state"] = "running"
            db.execute("UPDATE bilibili_jobs SET payload=?", (json.dumps(job),))
        restarted = BilibiliService(self.store, client_factory=self.client)
        self.addCleanup(restarted.close)
        saved = restarted.job(result["job_id"])
        self.assertEqual(saved["state"], "uncertain")
        self.assertEqual(saved["steps"]["metadata"], "sending")

    def test_different_service_same_runtime_has_operation_lock(self):
        second = BilibiliService(self.store, client_factory=self.client)
        self.addCleanup(second.close)
        with self.service.operation(), self.assertRaises(BilibiliError):
            with second.operation():
                self.fail("second process acquired the operation lock")

    def test_collection_changed_after_preview_stops_before_edit(self):
        plan = self.prepare()
        self.client.collection_members[20] = [{"id": 666, "aid": 456}]
        self.client.data["in_season"] = True
        result = self.apply(plan)
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("edit", self.client.calls)
        self.assertNotIn("remove", self.client.calls)

    def test_invalid_constant_and_foreign_collection_rejected(self):
        for changes in ({"constant": "99.9"}, {"section_id": 987}):
            with self.assertRaises(BilibiliError):
                self.service.prepare(preparation().model_copy(update=changes))
        self.assertEqual(self.client.calls, [])

    def test_definite_collection_failure_can_be_superseded_but_old_job_cannot_resume(self):
        def reject(*args):
            raise BilibiliError("rejected", status=409)

        self.client.add_episode = reject
        first = self.apply(self.prepare())
        self.assertEqual(first["state"], "failed")
        self.assertEqual(first["steps"]["metadata"], "verified")
        second = self.prepare()
        self.apply(second)
        self.assertEqual(self.service.job(first["job_id"])["state"], "superseded")
        with self.assertRaises(BilibiliError):
            self.service.retry(first["job_id"])

    def test_png_dimensions_and_invalid_data_fail_without_api_writes(self):
        for value in (png(1440), "data:image/png;base64,garbage", "https://example.com/a.png"):
            with self.assertRaises(BilibiliError):
                validate_png(value, 1920)
        self.assertEqual(self.client.calls, [])

    def test_route_bounds_and_secret_safe_validation(self):
        app = FastAPI()
        app.include_router(bilibili_router(self.service))
        with TestClient(app) as http:
            secret = "synthetic-private-token"
            response = http.post("/api/bilibili/prepare", json={"bvid": secret})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(secret, response.text)
            with patch("backend.bilibili.MAX_BODY", 20):
                self.assertEqual(
                    http.post("/api/bilibili/apply", content="x" * 21).status_code, 413
                )
            self.service.disconnect()
            self.assertEqual(http.get("/api/bilibili/videos/" + BV).status_code, 409)


class BilibiliProtocolTests(unittest.TestCase):
    def qr_client(self, data):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": data})

        client = BilibiliClient(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        return client, requests

    def test_qr_accepts_current_account_page_and_previous_passport_page(self):
        for page in (
            "https://account.bilibili.com/h5/account-h5/auth/scan-web",
            "https://passport.bilibili.com/h5-app/passport/login/scan",
        ):
            with self.subTest(page=page):
                data = {
                    "url": page + "?navhide=1&callback=&qrcode_key=synthetic-key",
                    "qrcode_key": "synthetic-key",
                }
                client, requests = self.qr_client(data)
                self.assertEqual(client.start_login(), data)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].method, "GET")
                self.assertEqual(
                    str(requests[0].url), PASSPORT + "/x/passport-login/web/qrcode/generate"
                )

    def test_qr_rejects_untrusted_or_malformed_page_without_echoing_secrets(self):
        path = "/h5/account-h5/auth/scan-web"
        for page in (
            "https://account.bilibili.com.evil.test" + path,
            "https://evilaccount.bilibili.com" + path,
            "http://account.bilibili.com" + path,
            "https://private-user:private-password@account.bilibili.com" + path,
            "https://account.bilibili.com:8443" + path,
            "https://account.bilibili.com:invalid" + path,
            "https://account.bilibili.com:99999" + path,
            "https://account.bilibili.com/other-path",
            "https://passport.bilibili.com" + path,
            "https://[invalid" + path,
            " https://account.bilibili.com" + path,
            "https://account.bilibili.com\n" + path,
        ):
            with self.subTest(page=page):
                client, requests = self.qr_client(
                    {"url": page + "?qrcode_key=private-key", "qrcode_key": "private-key"}
                )
                with self.assertRaises(BilibiliError) as error:
                    client.start_login()
                self.assertEqual(str(error.exception), "B 站没有返回有效登录二维码")
                self.assertEqual(len(requests), 1)

    def test_qr_rejects_invalid_response_field_types(self):
        url = "https://account.bilibili.com/h5/account-h5/auth/scan-web"
        for data in (
            None,
            [],
            ["private-key"],
            "private-key",
            {},
            {"url": None, "qrcode_key": "private-key"},
            {"url": 123, "qrcode_key": "private-key"},
            {"url": url},
            {"url": url, "qrcode_key": 123},
            {"url": url, "qrcode_key": ""},
            {"url": url, "qrcode_key": "  "},
        ):
            with self.subTest(data=data):
                client, _ = self.qr_client(data)
                with self.assertRaises(BilibiliError) as error:
                    client.start_login()
                self.assertEqual(str(error.exception), "B 站没有返回有效登录二维码")

    def test_edit_payload_preserves_real_web_shapes(self):
        payload = edit_payload(archive())
        self.assertEqual(payload["human_type2"], 1030)
        self.assertEqual(payload["creation_statement"], {"id": -1})
        self.assertEqual(payload["recreate"], -1)
        self.assertIsNone(payload["ab_cover_info"])
        self.assertEqual(payload["subtitle"], {"open": 0, "lan": ""})
        self.assertEqual(payload["space_hidden"], 2)
        self.assertEqual(payload["dolby"], 1)

    def test_special_submission_settings_are_rejected(self):
        for key in ("charging_pay", "interactive", "is_staff", "is_ab_cover"):
            data = archive()
            data["archive"][key] = 1
            with self.assertRaises(BilibiliError):
                edit_payload(data)

    def test_title_standard_markers_utage_mirror(self):
        chart = {"title": "Song", "difficulty_index": 3, "type": "standard"}
        self.assertEqual(
            title_for(chart, preparation(), "【maimai】Song[STD] Master13.9 AP"),
            "【maimai】Song[STD] Master13.9 AP",
        )
        utage = {"title": "Song", "difficulty_index": 5, "type": "utage"}
        body = preparation().model_copy(update={"constant": "13+?"})
        self.assertEqual(title_for(utage, body, ""), "【maimai】Song Utage13+? AP")
        mirror = preparation().model_copy(update={"bvid": "BV1bJouBSEiH"})
        self.assertEqual(title_for(chart, mirror, "REVERSED"), "REVERSED")

    def test_cdn_aliases_equal_but_external_host_not_equal(self):
        self.assertEqual(
            cover_identity("https://archive.biliimg.com/bfs/archive/hash.png"),
            cover_identity("http://i2.hdslb.com/bfs/archive/hash.png"),
        )
        self.assertNotEqual(
            cover_identity("https://evil.test/bfs/archive/hash.png"),
            cover_identity("https://i2.hdslb.com/bfs/archive/hash.png"),
        )

    def test_client_upload_accepts_biliimg_and_sends_multipart(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"url": "https://archive.biliimg.com/bfs/archive/new.png"},
                },
            )

        client = BilibiliClient(
            [{"name": "bili_jct", "value": "test-csrf", "domain": ".bilibili.com"}],
            transport=httpx.MockTransport(handle),
        )
        self.addCleanup(client.close)
        self.assertIn("biliimg", client.upload_cover("data:image/png;base64,aaa"))
        self.assertIn("multipart/form-data", requests[0].headers["content-type"])
        self.assertIn(b"test-csrf", requests[0].content)

    def test_client_error_never_echoes_response_or_token_or_url(self):
        for result in (
            {"code": -101, "message": "secret-session"},
            {"code": -400, "message": "secret-session"},
        ):
            client = BilibiliClient(
                transport=httpx.MockTransport(
                    lambda request, result=result: httpx.Response(200, json=result)
                )
            )
            try:
                with self.assertRaises(BilibiliError) as error:
                    client.request(
                        "POST", MEMBER, "/x/vu/web/edit", params={"csrf": "secret-session"}
                    )
                self.assertNotIn("secret-session", str(error.exception))
                self.assertNotEqual(error.exception.status, 401)
            finally:
                client.close()

    def test_client_timeout_marks_uncertain_and_never_retries(self):
        calls = []

        def handle(request):
            calls.append(request)
            raise httpx.ReadTimeout("secret-session")

        client = BilibiliClient(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        with self.assertRaises(BilibiliError) as error:
            client.request("POST", MEMBER, "/x/vu/web/edit")
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret-session", str(error.exception))

    def test_qr_uses_set_cookie_not_credential_redirect_and_verifies_account(self):
        seen = []

        def handle(request):
            seen.append(request)
            if request.url.path.endswith("/poll"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {"code": 0, "url": "https://evil.test/?SESSDATA=never-use"},
                    },
                    headers=[
                        (
                            "set-cookie",
                            "SESSDATA=test-session; Domain=.bilibili.com; Path=/; Secure",
                        ),
                        ("set-cookie", "bili_jct=test-csrf; Domain=.bilibili.com; Path=/; Secure"),
                    ],
                )
            return httpx.Response(
                200, json={"code": 0, "data": {"isLogin": True, "mid": 1234, "uname": "Test"}}
            )

        with tempfile.TemporaryDirectory() as root:
            store = FakeStore(root)
            service = BilibiliService(store, transport=httpx.MockTransport(handle))
            client = BilibiliClient(transport=httpx.MockTransport(handle))
            service._logins["opaque"] = {
                "client": client,
                "key": "private-key",
                "expires": 9999999999,
                "last_poll": 0,
                "state": "pending",
            }
            try:
                result = service.poll_login("opaque")
                self.assertEqual(result["state"], "connected")
                self.assertEqual(seen[-1].url.host, "api.bilibili.com")
                self.assertIn("SESSDATA=test-session", seen[-1].headers["cookie"])
                self.assertNotIn("never-use", service.session_path.read_text())
                self.assertNotIn("private-key", service.session_path.read_text())
            finally:
                service.close()

    def test_unowned_cdn_and_malformed_response_rejected(self):
        for url in ("https://biliimg.com.evil.test/a.png", "https://evilhdslb.com/a.png"):
            client = BilibiliClient(
                [{"name": "bili_jct", "value": "x", "domain": ".bilibili.com"}],
                transport=httpx.MockTransport(
                    lambda r, url=url: httpx.Response(200, json={"code": 0, "data": {"url": url}})
                ),
            )
            try:
                with self.assertRaises(BilibiliError):
                    client.upload_cover("image")
            finally:
                client.close()

    def test_client_redirect_not_followed_and_cookie_domain_guard(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://evil.test/"})

        client = BilibiliClient(
            [{"name": "SESSDATA", "value": "private", "domain": "evil.test"}],
            transport=httpx.MockTransport(handle),
        )
        self.addCleanup(client.close)
        with self.assertRaises(BilibiliError):
            client.request("GET", API, "/x/web-interface/nav")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("cookie", requests[0].headers)

    def test_owner_checked_against_archive_not_only_list(self):
        payload = archive()
        payload["archive"]["mid"] = 999
        client = BilibiliClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"code": 0, "data": payload})
            )
        )
        self.addCleanup(client.close)
        with self.assertRaises(BilibiliError):
            client.archive(BV, 1234)


if __name__ == "__main__":
    unittest.main()
