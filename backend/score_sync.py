"""Explicit Diving-Fish score pulls; the only personal score source."""

from __future__ import annotations

import copy
import threading
import time
from uuid import uuid4

import httpx

from .store import SyncBusyError, SyncTokenChangedError

RECORDS_ENDPOINT = "https://www.diving-fish.com/api/maimaidxprober/player/records"
JOB_COOLDOWN_SECONDS = 3
SHUTDOWN_GRACE_SECONDS = 40


class ScoreSyncError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)

    def detail(self):
        return {"code": self.code, "message": self.message}


def fetch_records(token):
    try:
        response = httpx.get(
            RECORDS_ENDPOINT,
            headers={"Import-Token": token},
            timeout=30,
            follow_redirects=False,
        )
    except httpx.RequestError:
        raise ScoreSyncError(502, "network_error", "暂时无法访问水鱼，本地记录已保留。") from None
    if response.status_code != 200:
        raise ScoreSyncError(
            502,
            "provider_error",
            f"水鱼返回 HTTP {response.status_code}，请检查 Token 或稍后重试。本地记录已保留。",
        )
    try:
        return response.json()
    except ValueError:
        raise ScoreSyncError(
            502, "invalid_response", "水鱼返回格式异常，本地记录已保留。"
        ) from None


class ScoreSync:
    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._thread = None
        self._closed = False
        self._job = None
        self._last_started = None

    def status(self):
        with self._lock:
            job = copy.deepcopy(self._job)
        return {
            "job": job,
            "divingfish": {
                "configured": bool(self.store.token()[0]),
                "last_sync": self.store.meta("last_divingfish_sync"),
            },
        }

    def start(self):
        if not self.store.token()[0]:
            raise ScoreSyncError(409, "no_token", "请先保存水鱼 Import-Token。")
        with self._lock:
            if self._closed:
                raise ScoreSyncError(503, "shutting_down", "服务正在重启，请稍后重试。")
            if self._job and self._job["state"] == "running":
                raise ScoreSyncError(409, "sync_busy", "水鱼同步正在进行，请等待完成。")
            if (
                self._last_started is not None
                and time.monotonic() - self._last_started < JOB_COOLDOWN_SECONDS
            ):
                raise ScoreSyncError(429, "sync_cooldown", "操作过于频繁，请稍后再试。")
            lease = self.store.sync_lease()
            try:
                token, generation = lease.__enter__()
            except SyncBusyError:
                raise ScoreSyncError(409, "sync_busy", "水鱼同步正在进行，请等待完成。") from None
            try:
                # Pending claims are frozen before the request; edits made during
                # the pull wait for the next sync.
                eligible = self.store.pending_snapshot()
                job = {
                    "id": str(uuid4()),
                    "kind": "divingfish",
                    "state": "running",
                    "stage": "fetching",
                    "message": "正在读取水鱼成绩",
                    "error": None,
                    "result": None,
                }
                self._job = job
                self._last_started = time.monotonic()
                self._thread = threading.Thread(
                    target=self._run,
                    args=(token, generation, eligible, lease),
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

    def _run(self, token, generation, eligible, lease):
        try:
            payload = fetch_records(token)
            self._update(stage="validating", message="正在核对并保存成绩")
            with self.store._token_lock:
                if generation != self.store._token_generation:
                    raise SyncTokenChangedError()
                result = self.store.import_provider(payload, eligible)
            self._update(state="succeeded", stage="completed", message="已完成", result=result)
        except ScoreSyncError as exc:
            self._fail(exc)
        except SyncTokenChangedError:
            self._fail(
                ScoreSyncError(
                    409, "token_changed", "同步期间 Token 已更改，请重新同步。本地记录已保留。"
                )
            )
        except ValueError as exc:
            message = (
                str(exc) if str(exc).startswith("水鱼") else "水鱼成绩格式异常，本地记录已保留。"
            )
            self._fail(ScoreSyncError(502, "invalid_scores", message))
        except Exception:
            self._fail(
                ScoreSyncError(500, "sync_failed", "同步未完成，请稍后重试。本地记录已保留。")
            )
        finally:
            token = None
            lease.__exit__(None, None, None)

    def _fail(self, error):
        self._update(state="failed", stage="failed", message=error.message, error=error.detail())

    def close(self):
        with self._lock:
            self._closed = True
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
