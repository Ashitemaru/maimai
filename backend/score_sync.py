"""Explicit game-score jobs; QR credentials live only in one short-lived worker."""

from __future__ import annotations

import copy
import json
import threading
import time
from uuid import uuid4

import httpx

from .ap_records import ScoreAccountError, ScoreSourceError, timestamp
from .maintenance import maintenance_status, utc_now
from .store import SyncBusyError, SyncTokenChangedError
from .wahlap import PAGINATION_REASONS

DIVINGFISH_ROOT = "https://www.diving-fish.com/api/maimaidxprober"
UPLOAD_ENDPOINT = DIVINGFISH_ROOT + "/player/update_records"
JOB_COOLDOWN_SECONDS = 3
SHUTDOWN_GRACE_SECONDS = 190


class ScoreSyncError(Exception):
    def __init__(
        self,
        status,
        code,
        message,
        *,
        retry_at=None,
        retry_after_seconds=None,
        stage=None,
        http_status=None,
        pagination_reason=None,
        logout_status=None,
        logout_confirmed=False,
    ):
        self.status = status
        self.code = code
        self.message = message
        self.retry_at = retry_at
        self.retry_after_seconds = retry_after_seconds
        self.stage = stage
        self.http_status = http_status
        self.pagination_reason = pagination_reason
        self.logout_status = logout_status
        self.logout_confirmed = logout_confirmed
        super().__init__(message)

    def detail(self):
        result = {"code": self.code, "message": self.message}
        if self.retry_at is not None:
            result.update(retry_at=self.retry_at, retry_after_seconds=self.retry_after_seconds)
        if self.stage in {"configuration", "authorization", "preview", "login", "scores"}:
            result["stage"] = self.stage
        if type(self.http_status) is int and 100 <= self.http_status <= 599:
            result["http_status"] = self.http_status
        if (
            self.code == "pagination_invalid"
            and isinstance(self.pagination_reason, str)
            and self.pagination_reason in PAGINATION_REASONS
        ):
            result["pagination_reason"] = self.pagination_reason
        if isinstance(self.logout_status, str) and self.logout_status in {
            "confirmed",
            "unverified",
            "failed",
            "not_attempted",
        }:
            confirmed = self.logout_status == "confirmed" and self.logout_confirmed is True
            result["logout_confirmed"] = confirmed
            result["logout_status"] = (
                "unverified"
                if self.logout_status == "confirmed" and not confirmed
                else self.logout_status
            )
        return result

    def headers(self):
        if self.retry_after_seconds is not None:
            return {"Retry-After": str(self.retry_after_seconds)}
        return None


def wahlap_available():
    try:
        from .wahlap import available

        return available()
    except (ImportError, OSError):
        return {"available": False, "reason": "华立取分服务尚未配置，请稍后再试。"}


def fetch_wahlap_scores(qr_text):
    from .wahlap import WahlapError, fetch_scores

    try:
        return fetch_scores(qr_text)
    except WahlapError as exc:
        message = str(exc)
        if exc.logout_status == "failed":
            message += "；华立退出请求未成功，请勿立即重复同步。"
        elif exc.logout_status == "unverified" and (
            exc.stage == "scores"
            or exc.code
            in (
                "invalid_response",
                "invalid_scores",
                "incomplete_scores",
                "pagination_invalid",
            )
        ):
            message += "；华立退出会话尚未确认，请勿立即重复同步。"
        raise ScoreSyncError(
            502,
            exc.code,
            message,
            stage=exc.stage,
            http_status=exc.http_status,
            pagination_reason=exc.pagination_reason,
            logout_status=exc.logout_status,
            logout_confirmed=exc.logout_confirmed,
        ) from None


def session_outcome(payload):
    confirmed = payload.get("logout_confirmed") is True
    if confirmed:
        return {"logout_confirmed": True, "logout_status": "confirmed", "notice": None}
    state = "failed" if payload.get("logout_status") == "failed" else "unverified"
    return {
        "logout_confirmed": False,
        "logout_status": state,
        "notice": "成绩已保存，但华立退出会话尚未确认；请勿立即重复同步。",
    }


def upload_records(records, music_data):
    """The upstream write API identifies charts by exact title and format, not ID.

    Resolve against DivingFish's public catalog at the explicit upload step. Report
    removed/unavailable charts as skipped; preserve the full local snapshot.
    """
    if not isinstance(music_data, list):
        raise ScoreSyncError(502, "upload_catalog", "水鱼曲库格式异常，尚未上传。")
    songs = {}
    title_ids = {}
    for row in music_data:
        if not isinstance(row, dict):
            continue
        song_id = str(row.get("id", ""))
        if song_id in songs:
            raise ScoreSyncError(502, "upload_catalog", "水鱼曲库存在重复编号，尚未上传。")
        songs[song_id] = row
        title, chart_type = row.get("title"), row.get("type")
        if isinstance(title, str) and chart_type in ("SD", "DX"):
            title_ids.setdefault((title, chart_type), set()).add(song_id)
    result = []
    skipped = []
    identities = set()
    for record in records:
        song = songs.get(str(record["song_id"]))
        difficulty = record["level_index"]
        if (
            not song
            or not 0 <= difficulty <= 5
            or not isinstance(song.get("title"), str)
            or not song["title"]
            or song.get("type") not in ("SD", "DX")
            or not isinstance(song.get("charts"), list)
            or difficulty >= len(song["charts"])
        ):
            skipped.append(
                {
                    "song_id": record["song_id"],
                    "level_index": difficulty,
                    "reason": "水鱼当前曲库无对应谱面",
                }
            )
            continue
        identity = (song["title"], song["type"], difficulty)
        if identity in identities or len(title_ids[identity[:2]]) != 1:
            raise ScoreSyncError(409, "upload_ambiguous", "水鱼曲目名称存在歧义，尚未上传。")
        identities.add(identity)
        result.append({**record, "title": song["title"], "type": song["type"]})
    if not result:
        raise ScoreSyncError(409, "upload_unmatched", "本次没有可对应水鱼的成绩，尚未上传。")
    return result, skipped


class ScoreSync:
    def __init__(self, store, *, clock=None):
        self.store = store
        self._clock = clock or utc_now
        self._lock = threading.Lock()
        self._thread = None
        self._closed = False
        self._probing = False
        self._probe_finished = threading.Event()
        self._probe_finished.set()
        self._job = None
        self._last_started = None
        with store.db() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS score_uploads("
                "id TEXT PRIMARY KEY,sync_id TEXT NOT NULL,attempted_at TEXT NOT NULL,"
                "ok INTEGER NOT NULL,receipt_json TEXT NOT NULL)"
            )

    def status(self):
        maintenance = maintenance_status(self._clock())
        with self._lock:
            job = copy.deepcopy(self._job)
            checking = self._probing
        if maintenance["active"]:
            capability = {"available": False, "reason": maintenance["message"]}
        elif checking:
            # The explicit check owns the worker; polling must not launch another.
            capability = {"available": True, "reason": None}
        else:
            capability = wahlap_available()
        last_sync = self.store.meta("last_wahlap_sync")
        return {
            "wahlap": {
                "available": capability.get("available") is True,
                "checking": checking,
                "reason": capability.get("reason"),
                "verified": bool(last_sync and last_sync.get("logout_confirmed")),
                "last_sync": last_sync,
                "has_snapshot": bool(last_sync),
                "maintenance": maintenance,
            },
            "job": job,
            "divingfish": {
                "configured": bool(self.store.token()[0]),
                "last_upload": self.store.meta("last_divingfish_upload"),
            },
        }

    def start_wahlap(self, qr_text):
        self._assert_start_allowed()
        # Keep the raw credential out of model/HTTP errors, jobs, SQLite and logs.
        from .wahlap import WahlapError, validate_sgid

        try:
            qr_text = validate_sgid(qr_text)
        except WahlapError:
            raise ScoreSyncError(
                422, "invalid_qr", "二维码内容无效，请重新扫描舞萌登录二维码。"
            ) from None
        capability = wahlap_available()
        if capability.get("available") is not True:
            raise ScoreSyncError(503, "wahlap_unavailable", "华立取分服务尚不可用，请稍后再试。")
        return self._start("wahlap", qr_text)

    def _assert_start_allowed(self):
        maintenance = maintenance_status(self._clock())
        if maintenance["active"]:
            raise ScoreSyncError(
                503,
                "maintenance",
                maintenance["message"],
                retry_at=maintenance["retry_at"],
                retry_after_seconds=maintenance["retry_after_seconds"],
            )

    def start_upload(self):
        if not self.store.meta("last_wahlap_sync"):
            raise ScoreSyncError(409, "no_snapshot", "请先从华立读取一次完整成绩。")
        if not self.store.token()[0]:
            raise ScoreSyncError(409, "no_token", "请先保存水鱼 Import-Token。")
        return self._start("divingfish_upload", None)

    def probe_wahlap(self):
        """Check the game configuration endpoint without consuming a login QR."""
        from .wahlap import WahlapError, probe_network

        self._assert_start_allowed()
        with self._lock:
            self._assert_idle()
            lease = self.store.sync_lease()
            try:
                lease.__enter__()
            except SyncBusyError:
                raise ScoreSyncError(409, "sync_busy", "成绩任务正在进行，请等待完成。") from None
            self._probing = True
            self._probe_finished.clear()
            self._last_started = time.monotonic()
        try:
            self._assert_start_allowed()
            return probe_network()
        except WahlapError as exc:
            raise ScoreSyncError(
                502,
                exc.code,
                str(exc),
                stage=exc.stage,
                http_status=exc.http_status,
            ) from None
        finally:
            lease.__exit__(None, None, None)
            with self._lock:
                self._probing = False
                self._probe_finished.set()

    def _assert_idle(self):
        # Called with _lock held by both sync and connectivity-check submissions.
        if self._closed:
            raise ScoreSyncError(503, "shutting_down", "服务正在重启，请稍后重试。")
        if self._probing:
            raise ScoreSyncError(409, "sync_busy", "正在检查华立连接，请等待完成。")
        if self._job and self._job["state"] == "running":
            raise ScoreSyncError(409, "sync_busy", "成绩任务正在进行，请等待完成。")
        if (
            self._last_started is not None
            and time.monotonic() - self._last_started < JOB_COOLDOWN_SECONDS
        ):
            raise ScoreSyncError(429, "sync_cooldown", "操作过于频繁，请稍后再试。")

    def _start(self, kind, credential):
        with self._lock:
            self._assert_idle()
            lease = self.store.sync_lease()
            try:
                token, generation = lease.__enter__()
            except SyncBusyError:
                raise ScoreSyncError(409, "sync_busy", "成绩任务正在进行，请等待完成。") from None
            try:
                eligible = self.store.pending_snapshot() if kind == "wahlap" else None
                job = {
                    "id": str(uuid4()),
                    "kind": kind,
                    "state": "running",
                    "stage": "starting",
                    "message": "正在准备",
                    "error": None,
                    "result": None,
                }
                self._job = job
                self._last_started = time.monotonic()
                self._thread = threading.Thread(
                    target=self._run,
                    args=(kind, credential, token, generation, eligible, lease),
                    daemon=True,
                    name="personal-score-sync",
                )
                self._thread.start()
            except Exception:
                lease.__exit__(None, None, None)
                raise
            return copy.deepcopy(job)

    def _update(self, **values):
        with self._lock:
            self._job.update(values)

    def _assert_open(self):
        with self._lock:
            if self._closed:
                raise ScoreSyncError(503, "interrupted", "同步已中断，请重新扫描二维码。")

    def _run(self, kind, credential, token, generation, eligible, lease):
        try:
            if kind == "wahlap":
                # A request accepted before 04:00 may reach its worker after 04:00.
                # Once fetch starts, allow it to finish, including session logout.
                self._assert_start_allowed()
                self._update(stage="fetching", message="正在读取华立成绩")
                payload = fetch_wahlap_scores(credential)
                credential = None
                if not isinstance(payload, dict) or payload.get("source") != "wahlap":
                    raise ValueError("invalid game response")
                self._update(stage="validating", message="正在核对并保存成绩")
                self._assert_open()
                result = self.store.import_provider(
                    {"records": payload.get("records")},
                    eligible,
                    source="wahlap",
                    account_id=payload.get("player_id"),
                    outcome=session_outcome(payload),
                )
            else:
                result = self._upload(token, generation)
            self._update(state="succeeded", stage="completed", message="已完成", result=result)
        except ScoreSyncError as exc:
            self._update(
                state="failed",
                stage="failed",
                message=exc.message,
                error=exc.detail(),
            )
        except ScoreAccountError:
            message = "二维码对应其他账号或账号信息无效，未修改本地记录。"
            self._update(
                state="failed",
                stage="failed",
                message=message,
                error={"code": "account_mismatch", "message": message},
            )
        except (ValueError, ScoreSourceError):
            message = "成绩格式未通过完整校验，未修改本地记录。"
            self._update(
                state="failed",
                stage="failed",
                message=message,
                error={"code": "invalid_scores", "message": message},
            )
        except Exception:
            message = "成绩任务未完成，请稍后重试。本地记录已保留。"
            self._update(
                state="failed",
                stage="failed",
                message=message,
                error={"code": "sync_failed", "message": message},
            )
        finally:
            credential = token = None
            lease.__exit__(None, None, None)

    def _upload(self, token, generation):
        self._update(stage="validating", message="正在准备上传水鱼")
        receipt = self.store.meta("last_wahlap_sync")
        if not receipt:
            raise ScoreSyncError(409, "no_snapshot", "请先从华立读取一次完整成绩。")
        sync_id = receipt["sync_id"]
        attempted_at = timestamp()
        try:
            with self.store.db() as conn:
                row = conn.execute(
                    "SELECT p.records_json FROM provider_syncs p JOIN score_sync_sources s "
                    "ON s.sync_id=p.id WHERE p.id=? AND s.source='wahlap'",
                    (sync_id,),
                ).fetchone()
            if row is None:
                raise ScoreSyncError(409, "no_snapshot", "完整成绩快照不存在，请重新读取华立。")
            records = json.loads(row[0])
            self._assert_open()
            response = httpx.get(
                DIVINGFISH_ROOT + "/music_data", timeout=20, follow_redirects=False
            )
            response.raise_for_status()
            body, skipped = upload_records(records, response.json())
            self._assert_open()
            with self.store._token_lock:
                if generation != self.store._token_generation or token != self.store.token()[0]:
                    raise SyncTokenChangedError()
            # The initiated request uses its captured token. Do not hold a local
            # settings lock while waiting on an external server: polling stays fast.
            self._update(stage="uploading", message="正在上传水鱼")
            response = httpx.post(
                UPLOAD_ENDPOINT,
                headers={"Import-Token": token},
                json=body,
                timeout=30,
                follow_redirects=False,
            )
            response.raise_for_status()
            reply = response.json()
            if not isinstance(reply, dict) or reply.get("message") != "更新成功":
                raise ScoreSyncError(502, "upload_rejected", "水鱼未确认上传成功，本地成绩已保留。")
            updated, created = reply.get("updates"), reply.get("creates")
            if (
                isinstance(updated, bool)
                or not isinstance(updated, int)
                or updated < 0
                or isinstance(created, bool)
                or not isinstance(created, int)
                or created < 0
                or updated + created != len(body)
            ):
                raise ScoreSyncError(
                    502, "upload_partial", "水鱼未确认全部成绩写入，本地成绩已保留。"
                )
            result = {
                "ok": True,
                "source": "divingfish_upload",
                "uploaded_at": timestamp(),
                "attempted_at": attempted_at,
                "records": len(body),
                "sync_id": sync_id,
                "uploaded_count": len(body),
                "snapshot_records": len(records),
                "skipped_count": len(skipped),
                "skipped": skipped[:50],
            }
            self._save_upload_receipt(result)
            return result
        except Exception as exc:
            if isinstance(exc, ScoreSyncError):
                error = exc
            elif isinstance(exc, SyncTokenChangedError):
                error = ScoreSyncError(409, "token_changed", "Token 已改变，尚未上传，请重新操作。")
            else:
                error = ScoreSyncError(
                    502, "upload_failed", "水鱼上传未获成功确认，本地成绩已保留。"
                )
            self._save_upload_receipt(
                {
                    "ok": False,
                    "source": "divingfish_upload",
                    "attempted_at": attempted_at,
                    "sync_id": sync_id,
                    "error": {"code": error.code, "message": error.message},
                }
            )
            raise error from None

    def _save_upload_receipt(self, result):
        data = json.dumps(result, ensure_ascii=False)
        with self.store.db() as conn:
            conn.execute(
                "INSERT INTO score_uploads VALUES(?,?,?,?,?)",
                (str(uuid4()), result["sync_id"], result["attempted_at"], int(result["ok"]), data),
            )
            conn.execute(
                "INSERT INTO metadata VALUES('last_divingfish_upload',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (data,),
            )

    def close(self):
        with self._lock:
            self._closed = True
            thread = self._thread
        if thread and thread is not threading.current_thread():
            # Let the bounded game worker attempt logout before process shutdown.
            thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
        self._probe_finished.wait(timeout=40)
