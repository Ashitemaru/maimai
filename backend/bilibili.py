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
from .bilibili_order import (
    ChartResolver,
    chart_identity,
    order_report,
    parsed_title,
    section_snapshot,
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
        token = re.match(r"^(?:\+PA|PA)(?=\s)", original)
        if token is None:
            raise BilibiliError("镜像稿件标题需以 PA 或 +PA 开头，请手动确认标题", status=422)
        title = ("+PA" if body.combo == "app" else "PA") + original[token.end() :]
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
                CREATE TABLE IF NOT EXISTS bilibili_chart_bindings (
                    account_mid INTEGER NOT NULL, bvid TEXT NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_mid,bvid)
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

    def bind_chart(
        self, account_mid, bvid, chart, observed_title, *, source="user", only_missing=False
    ):
        """Internal migration/user-selection hook; never imports an old candidate version."""
        binding = {
            "chart_key": chart["chart_key"],
            "chart_identity": chart_identity(chart),
            "title_identity": parsed_title(observed_title, bvid),
            "observed_title": observed_title,
            "source": source,
        }
        self._save_bindings(account_mid, {validate_bvid(bvid): binding}, only_missing=only_missing)
        return binding

    def _save_bindings(self, account_mid, bindings, *, only_missing=False):
        conflict = (
            "DO NOTHING"
            if only_missing
            else "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at"
        )
        with self.store.db() as db:
            for bvid, binding in bindings.items():
                db.execute(
                    f"INSERT INTO bilibili_chart_bindings VALUES (?,?,?,?) ON CONFLICT(account_mid,bvid) {conflict}",
                    (account_mid, bvid, json.dumps(binding), timestamp()),
                )

    def _resolver(self, client, account):
        with self.store.db() as db:
            bindings = {
                row["bvid"]: json.loads(row["payload"])
                for row in db.execute(
                    "SELECT bvid,payload FROM bilibili_chart_bindings WHERE account_mid=?",
                    (account["mid"],),
                )
            }
        latest = None

        def latest_title(bvid):
            nonlocal latest
            if latest is None:
                latest = {}
                for page in range(1, 101):
                    result = client.videos(page)
                    for item in result["items"]:
                        latest[item["bvid"]] = item["title"]
                    if not result["has_more"]:
                        break
                else:
                    raise BilibiliError("稿件数量超出排序核对上限", status=409)
            if bvid not in latest:
                latest[bvid] = client.archive(bvid, account["mid"])["archive"]["title"]
            return latest[bvid]

        return ChartResolver(self.store.all_charts(), bindings, latest_title)

    def collection_order(self):
        with self.operation(), self.client() as (client, account):
            if client.account()["mid"] != account["mid"]:
                raise BilibiliError("B 站账号已变化，请重新登录", status=409)
            resolver = self._resolver(client, account)
            reports = []
            for collection in client.collections()["items"]:
                for section in collection["sections"]:
                    snapshot = section_snapshot(
                        client.section(section["id"]),
                        section_id=section["id"],
                        season_id=collection["id"],
                    )
                    reports.append(
                        order_report(snapshot, resolver, season_title=collection["title"])
                    )
            return {"checked_at": timestamp(), "sections": reports}

    def _prepare_order(self, client, account, plan, collections):
        collection = next(
            c for c in collections if any(s["id"] == plan["section_id"] for s in c["sections"])
        )
        snapshot = section_snapshot(
            client.section(plan["section_id"]),
            section_id=plan["section_id"],
            season_id=collection["id"],
        )
        resolver = self._resolver(client, account)
        archive = plan["_archive"]
        target = {
            "aid": archive["archive"]["aid"],
            "cid": archive["videos"][0]["cid"],
            "bvid": plan["bvid"],
            "title": plan["title"],
        }
        plan["order"] = order_report(
            snapshot,
            resolver,
            season_title=collection["title"],
            target=target,
            explicit=plan["chart"],
        )
        keys = {item["chart_key"] for item in plan["order"]["desired"] if item["chart_key"]}
        catalog = {
            chart["chart_key"]: {
                "identity": chart_identity(chart),
                "version": chart.get("jp_release_version"),
            }
            for chart in resolver.charts.values()
            if chart["chart_key"] in keys
        }
        plan["_order"] = {
            "schema": 1,
            "snapshot": snapshot,
            "target": target,
            "desired_aids": [item["aid"] for item in plan["order"]["desired"]],
            "bindings": resolver.evidence,
            "catalog": catalog,
        }

    def _order_catalog_current(self, plan):
        if not plan.get("_order") or plan["_order"].get("schema") != 1:
            raise BilibiliError("此预览缺少合集排序核对，请重新生成预览", status=409)
        if plan["order"]["status"] == "blocked":
            raise BilibiliError("合集存在无法确定谱面或版本的稿件，请先完成对应", status=409)
        with self.store.db() as db:
            bindings = {
                row["bvid"]: json.loads(row["payload"])
                for row in db.execute(
                    "SELECT bvid,payload FROM bilibili_chart_bindings WHERE account_mid=?",
                    (plan["_account_mid"],),
                )
            }
        for bvid, expected in plan["_order"]["bindings"].items():
            if bvid in bindings and bindings[bvid]["chart_key"] != expected["chart_key"]:
                raise BilibiliError("稿件对应谱面的选择已变化，请重新生成预览", status=409)
        current = {chart["chart_key"]: chart for chart in self.store.all_charts()}
        for key, expected in plan["_order"]["catalog"].items():
            chart = current.get(key)
            if (
                not chart
                or {"identity": chart_identity(chart), "version": chart.get("jp_release_version")}
                != expected
            ):
                raise BilibiliError("参与排序的谱面信息已变化，请重新生成预览", status=409)

    def _order_snapshot(self, client, plan):
        header = plan["_order"]["snapshot"]["section"]
        return section_snapshot(
            client.section(header["id"]), section_id=header["id"], season_id=header["seasonId"]
        )

    def _order_titles_current(self, client, plan):
        resolver = self._resolver(client, {"mid": plan["_account_mid"]})
        resolver.bindings = plan["_order"]["bindings"]
        for episode in plan["_order"]["snapshot"]["episodes"]:
            if episode["bvid"] == plan["bvid"]:
                continue  # Full owned-archive revision checks protect the selected video.
            expected = plan["_order"]["bindings"].get(episode["bvid"])
            chart, problem = resolver.resolve(episode)
            if expected and (problem or not chart or chart["chart_key"] != expected["chart_key"]):
                raise BilibiliError("合集内稿件的真实标题已变化，请重新生成预览", status=409)

    def _order_preflight(self, client, plan):
        self._order_catalog_current(plan)
        self._order_titles_current(client, plan)
        if digest(self._order_snapshot(client, plan)) != plan["order"]["revision"]:
            raise BilibiliError("合集在预览后发生变化，请重新生成预览", status=409)

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
            # The user explicitly selected this chart; retaining this one binding
            # lets multiple unrecognized titles be resolved without any Bilibili write.
            self.bind_chart(account["mid"], body.bvid, chart, original, source="user")
            self._prepare_order(client, account, plan, collections)
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
            if not plan.get("_order") or plan["_order"].get("schema") != 1:
                raise BilibiliError("此预览缺少合集排序核对，请重新生成预览", status=409)
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
                if job["steps"].get("order") not in ("sending", "submitted", "verified"):
                    self._order_catalog_current(plan)
            else:
                if time.time() > expires:
                    raise BilibiliError("预览已过期，请重新生成", status=409)
                self._order_preflight(client, plan)
                with self.store.db() as db:
                    prior = db.execute(
                        "SELECT payload FROM bilibili_jobs WHERE account_mid=?", (account["mid"],)
                    ).fetchall()
                superseded = []
                for item in prior:
                    earlier = json.loads(item[0])
                    if earlier["state"] in ("complete", "superseded"):
                        continue
                    earlier_plan, _ = self._plan(earlier["plan_id"])

                    def touched(item):
                        ids = {item["section_id"]}
                        if item.get("_membership"):
                            ids.add(item["_membership"]["section_id"])
                        return ids

                    same_video = earlier["bvid"] == plan["bvid"]
                    same_section = bool(touched(earlier_plan) & touched(plan))
                    if not same_video and not same_section:
                        continue
                    steps = earlier["steps"]
                    unknown_write = any(
                        steps.get(key) in ("sending", "submitted")
                        for key in ("metadata", "remove_collection", "collection", "order")
                    )
                    if unknown_write or (
                        steps.get("metadata") == "verified" and earlier["state"] != "failed"
                    ):
                        raise BilibiliError("这条稿件还有未完成任务，请从处理记录继续", status=409)
                    if same_video:
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
            self._order_preflight(client, plan)
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
            self._order_preflight(client, plan)
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
        self._apply_order(client, plan, job)
        try:
            self._order_catalog_current(plan)
        except BilibiliError:
            pass  # A sent order can be acknowledged without replacing newer bindings.
        else:
            self._save_bindings(account["mid"], plan["_order"]["bindings"])
        job["state"] = "complete"
        self._save_job(job)

    def _order_members(self, current, plan, *, check_original_order):
        frozen = plan["_order"]
        original = frozen["snapshot"]
        if current["section"] != original["section"]:
            raise BilibiliError("合集小节设置已变化，请重新生成预览", status=409)
        expected_aids = set(frozen["desired_aids"])
        if {row["aid"] for row in current["episodes"]} != expected_aids:
            raise BilibiliError("合集稿件成员已变化，已停止排序", status=409)
        by_aid = {row["aid"]: row for row in current["episodes"]}
        target = frozen["target"]
        for row in original["episodes"]:
            actual = by_aid[row["aid"]]
            fields = (
                ("episode_id", "aid", "cid", "bvid")
                if row["aid"] == target["aid"]
                else ("episode_id", "aid", "cid", "bvid", "title")
            )
            if any(actual[key] != row[key] for key in fields):
                raise BilibiliError("合集稿件信息已变化，已停止排序", status=409)
        if any(by_aid[target["aid"]][key] != target[key] for key in ("aid", "cid", "bvid")):
            raise BilibiliError("加入合集的稿件身份不一致，已停止排序", status=409)
        if check_original_order:
            before = [row["aid"] for row in original["episodes"] if row["aid"] != target["aid"]]
            after = [row["aid"] for row in current["episodes"] if row["aid"] != target["aid"]]
            if before != after:
                raise BilibiliError("合集顺序已被其他操作修改，已停止排序", status=409)
        return [by_aid[aid]["episode_id"] for aid in frozen["desired_aids"]]

    def _apply_order(self, client, plan, job):
        stage = job["steps"].get("order")
        if stage not in ("sending", "submitted", "verified"):
            self._order_catalog_current(plan)
        current = self._order_snapshot(client, plan)
        expected_ids = self._order_members(
            current, plan, check_original_order=stage not in ("sending", "submitted", "verified")
        )
        actual_ids = [row["episode_id"] for row in current["episodes"]]
        if stage in ("sending", "submitted", "verified"):
            if actual_ids != job.get("_order_expected_ids"):
                raise BilibiliError(
                    "排序已发送，B 站尚未返回一致结果；不会重复提交", uncertain=True
                )
            job["steps"]["order"] = "verified"
            self._save_job(job)
            return
        if actual_ids == expected_ids:
            job["steps"]["order"] = "verified"
            job["_order_expected_ids"] = expected_ids
            self._save_job(job)
            return
        self._order_titles_current(client, plan)
        # Title reads may span pages; recheck members immediately before the write.
        fresh = self._order_snapshot(client, plan)
        if digest(fresh) != digest(current):
            raise BilibiliError("合集在排序核对时发生变化，请重新生成预览", status=409)
        job["_order_expected_ids"] = expected_ids
        job["steps"]["order"] = "sending"
        self._save_job(job)
        try:
            client.sort_section(current["section"], expected_ids)
        except BilibiliError as error:
            if not error.uncertain:
                job["steps"]["order"] = "rejected"
            raise
        job["steps"]["order"] = "submitted"
        self._save_job(job)
        current = self._order_snapshot(client, plan)
        self._order_members(current, plan, check_original_order=False)
        if [row["episode_id"] for row in current["episodes"]] != expected_ids:
            raise BilibiliError("排序已提交，等待 B 站返回更新结果", uncertain=True)
        job["steps"]["order"] = "verified"
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
        if job["steps"].get("collection") == "verified":
            raise BilibiliError("稿件已被其他操作移出目标合集，请重新生成预览", status=409)
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

    @router.get("/collections/order")
    def collection_order():
        return call(service.collection_order)

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
