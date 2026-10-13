"""Own-video cover/title/collection updates with frozen plans and durable receipts."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .bilibili_client import (
    BilibiliClient,
    BilibiliError,
    archive_revision,
    cover_identity,
    digest,
    edit_payload,
    validate_bvid,
)

MAX_BODY = 16 * 1024 * 1024
MIRROR_BVID = "BV1bJouBSEiH"
DIFFICULTIES = ("Basic", "Advanced", "Expert", "Master", "Re:Master")


class PrepareInput(BaseModel):
    model_config = {"extra": "forbid"}
    bvid: str = Field(min_length=12, max_length=12)
    chart_key: str = Field(min_length=1, max_length=80)
    achievement: str = Field(pattern=r"^(?:101|100|[0-9]{1,2})(?:\.[0-9]{1,4})?$")
    combo: Literal["ap", "app"]
    constant: str = Field(pattern=r"^(?:[0-9]{1,2}\.[0-9]|[0-9]{1,2}\+?\?)$")
    section_id: int = Field(gt=0, strict=True)
    mirror_horizontal: bool | None = None
    title_override: str | None = Field(default=None, min_length=1, max_length=80)
    include_type: bool | None = None
    version_override: str | None = Field(default=None, max_length=80)


class ApplyInput(BaseModel):
    model_config = {"extra": "forbid"}
    plan_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    cover16: str = Field(max_length=MAX_BODY)
    cover43: str = Field(max_length=MAX_BODY)


def timestamp():
    return datetime.now(UTC).isoformat()


def title_for(chart, body, original):
    if body.title_override is not None:
        title = body.title_override.strip()
    elif body.bvid == MIRROR_BVID:
        title = original
    else:
        is_utage = chart.get("type") == "utage" or chart.get("difficulty_index") == 5
        if is_utage:
            if not re.fullmatch(r"\d{1,2}\+?\?", body.constant):
                raise BilibiliError("宴谱面请填写 13? 或 13+? 形式的等级", status=422)
            difficulty = "Utage"
        else:
            index = chart.get("difficulty_index", chart.get("difficulty"))
            if index not in range(5) or not re.fullmatch(r"\d{1,2}\.\d", body.constant):
                raise BilibiliError("请选择明确的难度和一位小数定数", status=422)
            difficulty = DIFFICULTIES[index]
        marker = body.include_type
        if marker is None:
            marker = bool(
                re.search(r"\[(?:STD|DX)\]\s+(?:Basic|Advanced|Expert|Master|Re:Master)", original)
            )
        suffix = ""
        if marker and not is_utage:
            suffix = "[DX]" if chart.get("type") == "dx" else "[STD]"
        title = f"【maimai】{chart.get('display_title') or chart['title']}{suffix} {difficulty}{body.constant} {'AP+' if body.combo == 'app' else 'AP'}"
    if not title or len(title) > 80 or any(ord(c) < 32 for c in title):
        raise BilibiliError("标题必须为 1–80 个字符，且不能包含换行", status=422)
    return title


def validate_png(data_url, width):
    if not data_url.startswith("data:image/png;base64,"):
        raise BilibiliError("封面必须是 PNG 图片", status=422)
    try:
        encoded = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        if len(encoded) > 6 * 1024 * 1024:
            raise ValueError
        with Image.open(io.BytesIO(encoded)) as image:
            if (
                image.format != "PNG"
                or image.size != (width, 1080)
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ValueError
            image.verify()
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise BilibiliError(f"封面必须是 {width}×1080 的完整 PNG 图片", status=422) from None
    return hashlib.sha256(encoded).hexdigest()


class BilibiliService:
    def __init__(self, store, *, transport=None, client_factory=None):
        self.store = store
        self.session_path = store.runtime / "bilibili-session.json"
        self.transport = transport
        self.client_factory = client_factory or BilibiliClient
        self._lock = threading.Lock()
        self._logins = {}
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bilibili_plans (
                    plan_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    expires REAL NOT NULL, account_mid INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bilibili_jobs (
                    job_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL UNIQUE,
                    account_mid INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL, cover16 TEXT, cover43 TEXT
                );
            """)

        # If the process stopped after sending, preserve the per-step write marker.
        # A retry first reads back that step; it never repeats an uncertain edit.
        try:
            with self.operation(), store.db() as db:
                for row in db.execute("SELECT job_id,payload FROM bilibili_jobs").fetchall():
                    job = json.loads(row["payload"])
                    if job["state"] == "running":
                        job["state"] = "uncertain"
                        job["error"] = "上次处理已中断，请检查结果后继续"
                        db.execute(
                            "UPDATE bilibili_jobs SET payload=? WHERE job_id=?",
                            (json.dumps(job), row["job_id"]),
                        )
        except BilibiliError:
            pass  # Another process is still actively handling this account.

    def close(self):
        for entry in self._logins.values():
            entry["client"].close()
        self._logins.clear()

    @contextmanager
    def operation(self):
        if not self._lock.acquire(blocking=False):
            raise BilibiliError("另一项 B 站操作正在进行", status=409)
        try:
            path = self.store.runtime / ".bilibili-operation.lock"
            with path.open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise BilibiliError("另一项 B 站操作正在进行", status=409) from None
                try:
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            self._lock.release()

    def _session(self):
        try:
            value = json.loads(self.session_path.read_text())
            if not isinstance(value.get("account", {}).get("mid"), int) or not value.get("cookies"):
                return None
            return value
        except (OSError, ValueError, AttributeError):
            return None

    def _save_session(self, account, client):
        value = {"account": account, "cookies": client.cookies(), "saved_at": timestamp()}
        fd, path = tempfile.mkstemp(prefix=".bilibili-session-", dir=self.store.runtime)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.session_path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    @contextmanager
    def client(self):
        session = self._session()
        if session is None:
            raise BilibiliError("请先扫码登录 B 站", status=409)
        client = self.client_factory(session["cookies"], transport=self.transport)
        try:
            yield client, session["account"]
        finally:
            client.close()

    def status(self):
        session = self._session()
        return {
            "connected": session is not None,
            "account": session["account"] if session else None,
        }

    def start_login(self):
        with self.operation():
            self.close()
            client = self.client_factory(transport=self.transport)
            try:
                data = client.start_login()
            except Exception:
                client.close()
                raise
            login_id = uuid.uuid4().hex
            expires = time.time() + 180
            self._logins[login_id] = {
                "client": client,
                "key": data["qrcode_key"],
                "expires": expires,
                "last_poll": 0,
                "state": "pending",
            }
            return {
                "login_id": login_id,
                "url": data["url"],
                "expires_at": datetime.fromtimestamp(expires, UTC).isoformat(),
            }

    def poll_login(self, login_id):
        with self.operation():
            entry = self._logins.get(login_id)
            if not entry or entry["expires"] < time.time():
                if entry:
                    entry["client"].close()
                    del self._logins[login_id]
                return {"state": "expired"}
            if entry["state"] == "connected":
                return {"state": "connected", "account": self.status()["account"]}
            if time.time() - entry["last_poll"] < 2:
                return {"state": entry["state"]}
            entry["last_poll"] = time.time()
            data = entry["client"].poll_login(entry["key"])
            state = {86101: "pending", 86090: "scanned", 86038: "expired", 0: "connected"}.get(
                data.get("code")
            )
            if not state:
                raise BilibiliError("B 站登录没有完成，请重新扫码", status=409)
            if state == "connected":
                # Set-Cookie from the fixed passport host, never credentials in a redirect URL.
                client = entry["client"]
                client.csrf()
                account = client.account()
                self._save_session(account, client)
                entry["state"] = state
                entry["key"] = ""
                return {"state": state, "account": account}
            entry["state"] = state
            if state == "expired":
                entry["client"].close()
                del self._logins[login_id]
            return {"state": state}

    def disconnect(self):
        with self.operation():
            self.close()
            self.session_path.unlink(missing_ok=True)
        return {"connected": False, "account": None}

    def videos(self, page):
        with self.client() as (client, _):
            return client.videos(page)

    def video(self, bvid):
        with self.client() as (client, account):
            data = client.archive(bvid, account["mid"])
        archive = data["archive"]
        return {key: archive.get(key, "") for key in ("bvid", "title", "cover", "cover43")} | {
            "revision": archive_revision(data),
            "mirror_horizontal": bvid == MIRROR_BVID,
        }

    def collections(self):
        with self.client() as (client, _):
            return client.collections()

    def prepare(self, body):
        validate_bvid(body.bvid)
        chart = self.store.chart(body.chart_key)
        if not chart:
            raise BilibiliError("曲库中找不到这张谱面", status=422)
        achievement = float(body.achievement)
        if not 0 <= float(body.constant.rstrip("+?")) <= 20:
            raise BilibiliError("定数不在有效范围内", status=422)
        if not 0 <= achievement <= 101:
            raise BilibiliError("达成率不在有效范围内", status=422)
        with self.operation(), self.client() as (client, account):
            if client.account()["mid"] != account["mid"]:
                raise BilibiliError("B 站登录账号已变化，请重新登录", status=409)
            data = client.archive(body.bvid, account["mid"])
            collections = client.collections()["items"]
            sections = {s["id"]: s for c in collections for s in c["sections"]}
            if body.section_id not in sections:
                raise BilibiliError("请选择当前账号的合集小节", status=422)
            original = data["archive"]["title"]
            membership = client.membership(data, sections)
            plan_id = uuid.uuid4().hex
            expires = time.time() + 3600
            plan = {
                "plan_id": plan_id,
                "bvid": body.bvid,
                "old_title": original,
                "title": title_for(chart, body, original),
                "chart": chart,
                "achievement": f"{achievement:.4f}",
                "combo": body.combo,
                "constant": body.constant,
                "version_override": body.version_override,
                "mirror_horizontal": body.mirror_horizontal
                if body.mirror_horizontal is not None
                else body.bvid == MIRROR_BVID,
                "section_id": body.section_id,
                "section_title": sections[body.section_id]["title"],
                "revision": archive_revision(data),
                "expires_at": datetime.fromtimestamp(expires, UTC).isoformat(),
                "_archive": data,
                "_membership": membership,
                "_account_mid": account["mid"],
            }
            with self.store.db() as db:
                db.execute(
                    "DELETE FROM bilibili_plans WHERE expires < ? AND plan_id NOT IN (SELECT plan_id FROM bilibili_jobs)",
                    (time.time(),),
                )
                db.execute(
                    "INSERT INTO bilibili_plans VALUES (?,?,?,?,?)",
                    (plan_id, timestamp(), expires, account["mid"], json.dumps(plan)),
                )
            return self.public_plan(plan)

    @staticmethod
    def public_plan(plan):
        return {key: value for key, value in plan.items() if not key.startswith("_")}

    def _plan(self, plan_id):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM bilibili_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise BilibiliError("预览已不存在，请重新生成", status=404)
        return json.loads(row["payload"]), row["expires"]

    def _save_job(self, job):
        job["updated_at"] = timestamp()
        with self.store.db() as db:
            db.execute(
                "UPDATE bilibili_jobs SET payload=?,updated_at=? WHERE job_id=?",
                (json.dumps(job), job["updated_at"], job["job_id"]),
            )
            if job["state"] == "complete":
                db.execute(
                    "UPDATE bilibili_jobs SET cover16=NULL,cover43=NULL WHERE job_id=?",
                    (job["job_id"],),
                )

    @staticmethod
    def public_job(job):
        return {key: value for key, value in job.items() if not key.startswith("_")}

    def jobs(self):
        session = self._session()
        if not session:
            return {"items": []}
        with self.store.db() as db:
            rows = db.execute(
                "SELECT payload FROM bilibili_jobs WHERE account_mid=? ORDER BY updated_at DESC LIMIT 30",
                (session["account"]["mid"],),
            ).fetchall()
        return {"items": [self.public_job(json.loads(row[0])) for row in rows]}

    def job(self, job_id):
        session = self._session()
        with self.store.db() as db:
            row = db.execute(
                "SELECT payload,account_mid,cover16,cover43 FROM bilibili_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if not row or not session or session["account"]["mid"] != row["account_mid"]:
            raise BilibiliError("找不到这项投稿处理记录", status=404)
        job = json.loads(row["payload"])
        plan, _ = self._plan(job["plan_id"])
        result = self.public_job(job) | {"plan": self.public_plan(plan)}
        if row["cover16"] and row["cover43"]:
            result["covers"] = {"cover16": row["cover16"], "cover43": row["cover43"]}
        return result

    def retry(self, job_id):
        public = self.job(job_id)
        if public["state"] == "complete":
            return public
        if public["state"] == "superseded":
            raise BilibiliError("该任务已由新的预览取代", status=409)
        with self.store.db() as db:
            row = db.execute(
                "SELECT plan_id,cover16,cover43 FROM bilibili_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if not row["cover16"] or not row["cover43"]:
            raise BilibiliError("任务图片已不存在，请重新生成预览", status=409)
        return self.apply(
            ApplyInput(plan_id=row["plan_id"], cover16=row["cover16"], cover43=row["cover43"])
        )

    def apply(self, body):
        hashes = {
            "cover16": validate_png(body.cover16, 1920),
            "cover43": validate_png(body.cover43, 1440),
        }
        with self.operation(), self.client() as (client, account):
            plan, expires = self._plan(body.plan_id)
            if account["mid"] != plan["_account_mid"] or client.account()["mid"] != account["mid"]:
                raise BilibiliError("预览属于另一个 B 站账号，请重新生成", status=409)
            with self.store.db() as db:
                row = db.execute(
                    "SELECT payload FROM bilibili_jobs WHERE plan_id=?", (body.plan_id,)
                ).fetchone()
            if row:
                job = json.loads(row[0])
                if job["hashes"] != hashes:
                    raise BilibiliError("图片与已提交的预览不一致，请重新生成预览", status=409)
                if job["state"] == "complete":
                    return self.public_job(job)
                if job["state"] == "superseded":
                    raise BilibiliError("该任务已由新的预览取代", status=409)
            else:
                if time.time() > expires:
                    raise BilibiliError("预览已过期，请重新生成", status=409)
                with self.store.db() as db:
                    prior = db.execute(
                        "SELECT payload FROM bilibili_jobs WHERE account_mid=?", (account["mid"],)
                    ).fetchall()
                superseded = []
                for item in prior:
                    earlier = json.loads(item[0])
                    if earlier["bvid"] != plan["bvid"] or earlier["state"] in (
                        "complete",
                        "superseded",
                    ):
                        continue
                    steps = earlier["steps"]
                    unknown_write = any(
                        steps.get(key) in ("sending", "submitted")
                        for key in ("metadata", "remove_collection", "collection")
                    )
                    if unknown_write or (
                        steps.get("metadata") == "verified" and earlier["state"] != "failed"
                    ):
                        raise BilibiliError("这条稿件还有未完成任务，请从处理记录继续", status=409)
                    superseded.append(earlier)
                job = {
                    "job_id": uuid.uuid4().hex,
                    "plan_id": body.plan_id,
                    "bvid": plan["bvid"],
                    "title": plan["title"],
                    "state": "running",
                    "steps": {},
                    "hashes": hashes,
                    "error": None,
                    "updated_at": timestamp(),
                    "_urls": {},
                }
                with self.store.db() as db:
                    for earlier in superseded:
                        earlier["state"] = "superseded"
                        db.execute(
                            "UPDATE bilibili_jobs SET payload=?,cover16=NULL,cover43=NULL WHERE job_id=?",
                            (json.dumps(earlier), earlier["job_id"]),
                        )
                    db.execute(
                        "INSERT INTO bilibili_jobs VALUES (?,?,?,?,?,?,?)",
                        (
                            job["job_id"],
                            body.plan_id,
                            account["mid"],
                            job["updated_at"],
                            json.dumps(job),
                            body.cover16,
                            body.cover43,
                        ),
                    )
            try:
                job["error"] = None
                self._apply(client, account, plan, job, body)
            except BilibiliError as error:
                job["error"] = str(error)
                job["state"] = "uncertain" if error.uncertain else "failed"
                self._save_job(job)
            except Exception:
                # A programming/persistence error after a send must not turn into a blind retry.
                job["error"] = "处理意外中断，请刷新查看结果后重试"
                job["state"] = "uncertain"
                self._save_job(job)
            return self.public_job(job)

    def _apply(self, client, account, plan, job, body):
        data = client.archive(plan["bvid"], account["mid"])
        edit_state = job["steps"].get("metadata")
        if edit_state in ("sending", "submitted", "verified"):
            if not self._matches(data, plan, job):
                raise BilibiliError(
                    "修改已发送，B 站暂未返回一致结果；请稍后检查，不会重复提交",
                    status=409,
                    uncertain=True,
                )
            job["steps"]["metadata"] = "verified"
            self._save_job(job)
        else:
            if archive_revision(data) != plan["revision"]:
                raise BilibiliError("稿件在预览后发生变化，请重新生成预览", status=409)
            for ratio, field in (("cover16", "cover"), ("cover43", "cover43")):
                if field not in job["_urls"]:
                    job["steps"][ratio] = "sending"
                    self._save_job(job)
                    job["_urls"][field] = client.upload_cover(getattr(body, ratio))
                    job["steps"][ratio] = "uploaded"
                    self._save_job(job)
            # Cover uploads can take time; check again immediately before editing.
            data = client.archive(plan["bvid"], account["mid"])
            if archive_revision(data) != plan["revision"]:
                raise BilibiliError("稿件在上传封面时发生变化，请重新生成预览", status=409)
            sections = {s["id"] for c in client.collections()["items"] for s in c["sections"]}
            if (
                plan["section_id"] not in sections
                or client.membership(data, sections) != plan["_membership"]
            ):
                raise BilibiliError("稿件所属合集在预览后发生变化，请重新生成预览", status=409)
            payload = edit_payload(data)
            payload.update(title=plan["title"], **job["_urls"])
            job["steps"]["metadata"] = "sending"
            self._save_job(job)
            try:
                client.edit(payload)
            except BilibiliError as error:
                if not error.uncertain:
                    job["steps"]["metadata"] = "rejected"
                raise
            job["steps"]["metadata"] = "submitted"
            self._save_job(job)
            data = client.archive(plan["bvid"], account["mid"])
            if not self._matches(data, plan, job):
                raise BilibiliError("修改已提交，等待 B 站返回更新结果", uncertain=True)
            job["steps"]["metadata"] = "verified"
            self._save_job(job)
        self._collection(client, plan, job, data)
        job["state"] = "complete"
        self._save_job(job)

    @staticmethod
    def _matches(data, plan, job):
        current = edit_payload(data)
        expected = edit_payload(plan["_archive"])
        expected.update(title=plan["title"], **job["_urls"])
        for key in ("cover", "cover43"):
            if cover_identity(current.pop(key, "")) != cover_identity(expected.pop(key, "")):
                return False
        return digest(current) == digest(expected)

    def _collection(self, client, plan, job, data):
        section_id = plan["section_id"]
        aid = data["archive"]["aid"]
        sections = {s["id"] for c in client.collections()["items"] for s in c["sections"]}
        if section_id not in sections:
            raise BilibiliError("目标合集已变化，请重新生成预览", status=409)
        target = client.section(section_id)
        if any(e.get("aid") == aid for e in target.get("episodes") or []):
            job["steps"]["collection"] = "verified"
            return
        if job["steps"].get("collection") in ("sending", "submitted"):
            raise BilibiliError("合集修改结果尚未确认；请稍后检查，不会重复提交", uncertain=True)
        membership = plan["_membership"]
        current_membership = client.membership(data, sections)
        if current_membership != membership:
            removed = job["steps"].get("remove_collection") in ("sending", "submitted", "verified")
            if not (removed and current_membership is None):
                raise BilibiliError("稿件所属合集已被其他操作修改，请在 B 站检查", status=409)
        if membership and membership["section_id"] != section_id:
            if membership["section_id"] not in sections:
                raise BilibiliError("原合集已变化，请在 B 站检查", status=409)
            previous = client.section(membership["section_id"])
            episode = next((e for e in previous.get("episodes") or [] if e.get("aid") == aid), None)
            if episode:
                if episode["id"] != membership["episode_id"]:
                    raise BilibiliError("稿件所属合集已变化，请重新生成预览", status=409)
                if job["steps"].get("remove_collection") in ("sending", "submitted"):
                    raise BilibiliError("移出原合集的结果尚未确认，不会重复提交", uncertain=True)
                job["steps"]["remove_collection"] = "sending"
                self._save_job(job)
                try:
                    client.remove_episode(episode["id"])
                except BilibiliError as error:
                    if not error.uncertain:
                        job["steps"]["remove_collection"] = "rejected"
                    raise
                job["steps"]["remove_collection"] = "submitted"
                self._save_job(job)
                previous = client.section(membership["section_id"])
                if any(e.get("aid") == aid for e in previous.get("episodes") or []):
                    raise BilibiliError("已请求移出原合集，等待 B 站更新", uncertain=True)
            job["steps"]["remove_collection"] = "verified"
            self._save_job(job)
        job["steps"]["collection"] = "sending"
        self._save_job(job)
        try:
            client.add_episode(section_id, data)
        except BilibiliError as error:
            if not error.uncertain:
                job["steps"]["collection"] = "rejected"
            raise
        job["steps"]["collection"] = "submitted"
        self._save_job(job)
        target = client.section(section_id)
        if not any(e.get("aid") == aid for e in target.get("episodes") or []):
            raise BilibiliError("已请求加入合集，等待 B 站更新", uncertain=True)
        job["steps"]["collection"] = "verified"


async def input_body(request, model, maximum):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > maximum:
            raise HTTPException(413, "请求内容过大")
    try:
        return model.model_validate_json(content)
    except ValidationError:
        raise HTTPException(422, "投稿参数无效，请检查输入") from None


def bilibili_router(service):
    router = APIRouter(prefix="/api/bilibili", tags=["bilibili"])

    def call(method, *args):
        try:
            return method(*args)
        except BilibiliError as error:
            raise HTTPException(error.status, str(error)) from None

    @router.get("/status")
    def status():
        return call(service.status)

    @router.post("/login")
    def login():
        return call(service.start_login)

    @router.get("/login/{login_id}")
    def poll(login_id: str):
        return call(service.poll_login, login_id)

    @router.delete("/session")
    def disconnect():
        return call(service.disconnect)

    @router.get("/videos")
    def videos(page: int = Query(default=1, ge=1, le=100)):
        return call(service.videos, page)

    @router.get("/videos/{bvid}")
    def video(bvid: str):
        return call(service.video, bvid)

    @router.get("/collections")
    def collections():
        return call(service.collections)

    @router.post("/prepare")
    async def prepare(request: Request):
        body = await input_body(request, PrepareInput, 16 * 1024)
        return await run_in_threadpool(call, service.prepare, body)

    @router.post("/apply")
    async def apply(request: Request):
        body = await input_body(request, ApplyInput, MAX_BODY)
        return await run_in_threadpool(call, service.apply, body)

    @router.get("/jobs")
    def jobs():
        return call(service.jobs)

    @router.get("/jobs/{job_id}")
    def job(job_id: str):
        return call(service.job, job_id)

    @router.post("/jobs/{job_id}/retry")
    def retry(job_id: str):
        return call(service.retry, job_id)

    return router
