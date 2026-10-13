"""Bounded, sandboxed access to the separately installed Wahlap score runtime."""

from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import math
import os
import platform
import re
import selectors
import shutil
import signal
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

UPSTREAM_COMMIT = "4aa10aea4e559b53e939d9013bde419a33417067"
REPO = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO / "deploy" / "wahlap-runtime.lock.json"
MAX_RECORDS = 20000
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
WORKER_TIMEOUT = 180
NETWORK_PROBE_TIMEOUT = 30
ERROR_STAGES = frozenset({"configuration", "authorization", "preview", "login", "scores"})
ERROR_MESSAGES = {
    "disabled": "华立同步尚未启用",
    "runtime_unavailable": "华立取分运行环境尚未就绪",
    "runtime_integrity": "华立取分运行环境校验失败",
    "sandbox_unavailable": "华立取分隔离环境不可用",
    "service_unavailable": "华立同步服务尚未就绪",
    "invalid_qr": "请提供完整的 SGWCMAID 玩家二维码文本",
    "busy": "已有华立同步正在进行，请稍后重试",
    "worker_timeout": "华立取分超时，会话状态未确认，请勿立即重复同步",
    "worker_failed": "华立取分失败，会话状态未确认",
    "invalid_response": "华立取分返回格式异常，未导入成绩",
    "invalid_scores": "华立成绩校验失败，未导入成绩",
    "incomplete_scores": "华立返回的成绩字段不完整，未导入成绩",
    "pagination_invalid": "华立成绩分页异常，未导入成绩",
    "session_active": "账号仍在游戏中，请结束游戏后再同步",
    "session_state_unknown": "暂时无法确认账号是否已退出游戏，未发起登录",
    "authorization_failed": "二维码授权失败或已过期，请重新获取",
    "login_failed": "华立未确认本次登录，未读取成绩或强制退出",
    "login_state_unknown": "本次登录结果未确认，请勿立即重复同步",
    "network_error": "华立暂时不可访问，请稍后重试",
    "upstream_empty_response": "华立返回空响应，请检查取分代理出口",
    "upstream_http_error": "华立接口返回 HTTP 错误，请稍后重试",
    "upstream_decode_error": "华立接口返回内容无法解析，未导入成绩",
    "upstream_timeout": "华立接口连接超时，请检查取分代理出口",
    "upstream_transport_error": "华立接口连接失败，请检查取分代理出口",
    "invalid_endpoint": "华立返回的接口地址未通过校验",
}
LOGOUT_NOTICES = {
    "confirmed": None,
    "unverified": "成绩已读取，华立退出会话尚未确认；请勿立即重复同步。",
    "failed": "成绩已读取，但华立退出请求未成功；请勿立即重复同步。",
}


class WahlapError(RuntimeError):
    """Only fixed messages may cross the process/API boundary."""

    def __init__(self, code: str, *, stage=None, http_status=None):
        self.code = code if isinstance(code, str) and code in ERROR_MESSAGES else "worker_failed"
        self.stage = stage if isinstance(stage, str) and stage in ERROR_STAGES else None
        self.http_status = (
            http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        )
        self.logout_status = "unverified"
        self.logout_confirmed = False
        super().__init__(ERROR_MESSAGES[self.code])


def error_response(error: WahlapError) -> dict:
    response = {
        "ok": False,
        "error": error.code,
        "logout_status": error.logout_status,
        "logout_confirmed": error.logout_confirmed,
    }
    if error.stage is not None:
        response["stage"] = error.stage
    if error.http_status is not None:
        response["http_status"] = error.http_status
    return response


def validate_sgid(value: object) -> str:
    if not isinstance(value, str):
        raise WahlapError("invalid_qr")
    value = value.strip()
    if not re.fullmatch(r"SGWCMAID[0-9]{12}[A-Za-z0-9+/=_-]{1,2028}", value):
        raise WahlapError("invalid_qr")
    return value


def _runtime_dir() -> Path:
    return Path(os.environ.get("MAIMAI_WAHLAP_RUNTIME_DIR", "/opt/mai-journal-wahlap"))


def _state_dir() -> Path:
    return Path(
        os.environ.get("MAIMAI_WAHLAP_STATE_DIR", "/srv/mai-journal/shared/runtime/wahlap-worker")
    )


def verify_runtime(runtime: Path) -> dict:
    try:
        lock = json.loads(LOCK_PATH.read_text())
        installed = json.loads((runtime / "runtime.json").read_text())
        if lock["commit"] != UPSTREAM_COMMIT or installed != lock:
            raise WahlapError("runtime_integrity")
        for relative, expected in lock["files"].items():
            path = runtime / "upstream" / relative
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise WahlapError("runtime_integrity")
        return lock
    except (OSError, ValueError, KeyError, TypeError):
        raise WahlapError("runtime_integrity") from None


def _configured_paths() -> tuple[Path, Path, str, str]:
    if os.environ.get("MAIMAI_WAHLAP_ENABLED") != "1":
        raise WahlapError("disabled")
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise WahlapError("runtime_unavailable")
    runtime, state = _runtime_dir().resolve(), _state_dir().resolve()
    wine = shutil.which(os.environ.get("MAIMAI_WAHLAP_WINE", "wine"))
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise WahlapError("sandbox_unavailable")
    if not wine or not (runtime / ".venv/bin/python").is_file():
        raise WahlapError("runtime_unavailable")
    if not state.is_dir() or not os.access(state, os.W_OK):
        raise WahlapError("runtime_unavailable")
    if state == runtime or runtime in state.parents or state in runtime.parents:
        raise WahlapError("runtime_unavailable")
    verify_runtime(runtime)
    # Resolve the entry point; Wine's secondary launchers still need /etc/alternatives.
    wine = str(Path(wine).resolve())
    if not Path(wine).is_relative_to("/usr"):
        raise WahlapError("runtime_unavailable")
    return runtime, state, wine, bwrap


def _local_available() -> dict:
    """Check files and a credential-free sandbox probe; never fall back outside it."""
    try:
        paths = _configured_paths()
        if not _sandbox_available(*paths, int(time.monotonic() // 60)):
            raise WahlapError("sandbox_unavailable")
    except WahlapError as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "reason": None}


def _socket_path() -> str:
    return os.environ.get("MAIMAI_WAHLAP_SOCKET", "/run/mai-journal-wahlap.sock")


def _response_error(result: dict) -> WahlapError:
    error = WahlapError(
        result.get("error", "worker_failed"),
        stage=result.get("stage"),
        http_status=result.get("http_status"),
    )
    status = result.get("logout_status")
    if isinstance(status, str) and status in (*LOGOUT_NOTICES, "not_attempted"):
        error.logout_status = status
        error.logout_confirmed = status == "confirmed" and result.get("logout_confirmed") is True
    return error


def _socket_request(payload: dict, *, timeout: float = WORKER_TIMEOUT + 10) -> dict:
    """The web process never launches Wine or relaxes its own systemd hardening."""
    encoded = json.dumps(payload).encode()
    if len(encoded) > 4096:
        raise WahlapError("invalid_qr")
    data = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(_socket_path())
            connection.sendall(encoded)
            connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WahlapError("worker_timeout")
                connection.settimeout(remaining)
                block = connection.recv(65536)
                if not block:
                    break
                data.extend(block)
                if len(data) > MAX_OUTPUT_BYTES:
                    raise WahlapError("invalid_response")
    except TimeoutError:
        raise WahlapError("worker_timeout") from None
    except OSError:
        raise WahlapError("service_unavailable") from None
    try:
        response = json.loads(data)
    except (ValueError, UnicodeError):
        raise WahlapError("invalid_response") from None
    if not isinstance(response, dict):
        raise WahlapError("invalid_response")
    if response.get("ok") is not True:
        raise _response_error(response)
    return response


@functools.lru_cache(maxsize=4)
def _service_available(path: str, minute: int) -> dict:
    try:
        response = _socket_request({"kind": "probe"}, timeout=15)
        if response.get("available") is True and response.get("reason") is None:
            return {"available": True, "reason": None}
        reason = response.get("reason")
        if response.get("available") is not False or reason not in ERROR_MESSAGES.values():
            raise WahlapError("invalid_response")
        return {"available": False, "reason": reason}
    except WahlapError as exc:
        return {"available": False, "reason": str(exc)}


def available() -> dict:
    if os.environ.get("MAIMAI_WAHLAP_ENABLED") != "1":
        return {"available": False, "reason": ERROR_MESSAGES["disabled"]}
    return dict(_service_available(_socket_path(), int(time.monotonic() // 60)))


def _validate_network_probe(result: dict) -> dict:
    if result.get("probe") != "network" or result.get("account_access") is not False:
        raise WahlapError("invalid_response")
    return {"ok": True, "probe": "network", "account_access": False}


def _probe_failure(error: WahlapError) -> WahlapError:
    if error.code == "worker_timeout":
        error = WahlapError("upstream_timeout", stage="configuration")
    error.logout_status = "not_attempted"
    error.logout_confirmed = False
    return error


def probe_network() -> dict:
    """Explicit, uncached configuration check; never reads or resolves a player's QR."""
    if os.environ.get("MAIMAI_WAHLAP_ENABLED") != "1":
        raise WahlapError("disabled")
    try:
        return _validate_network_probe(
            _socket_request({"kind": "network_probe"}, timeout=NETWORK_PROBE_TIMEOUT + 5)
        )
    except WahlapError as exc:
        raise _probe_failure(exc) from None


def _probe_network_local() -> dict:
    try:
        runtime, state, wine, bwrap = _configured_paths()
        with _single_worker(state):
            result = _run_worker(
                _sandbox_command(runtime, state, wine, bwrap) + ["--probe-network"],
                {},
                timeout=NETWORK_PROBE_TIMEOUT,
            )
        return _validate_network_probe(result)
    except WahlapError as exc:
        raise _probe_failure(exc) from None


@functools.lru_cache(maxsize=4)
def _sandbox_available(runtime, state, wine, bwrap, minute: int) -> bool:
    try:
        command = _sandbox_command(runtime, state, wine, bwrap) + ["--probe"]
        return _run_worker(command, {}, timeout=10).get("probe") == "imports"
    except WahlapError:
        return False


def _sandbox_command(runtime: Path, state: Path, wine: str, bwrap: str) -> list[str]:
    command = [
        bwrap,
        "--die-with-parent",
        "--unshare-all",
        "--unshare-user",
        "--share-net",
        "--new-session",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--disable-userns",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    for path in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    for path in (
        "/etc/ssl",
        "/etc/alternatives",
        "/etc/fonts",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
    ):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    command += [
        "--ro-bind",
        str(runtime),
        "/runtime",
        "--ro-bind",
        str(REPO / "backend"),
        "/app/backend",
        "--ro-bind",
        str(LOCK_PATH),
        "/app/deploy/wahlap-runtime.lock.json",
        "--bind",
        str(state),
        "/state",
        "--chdir",
        "/state",
    ]
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/state/home",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "MAIMAI_WAHLAP_RUNTIME_DIR": "/runtime",
        "MAIMAI_WAHLAP_STATE_DIR": "/state",
        "MAIMAI_CHIME_WINE_BINARY": wine,
        "MAIMAI_CHIME_WINEPREFIX": "/state/wineprefix",
        "WINEPREFIX": "/state/wineprefix",
        "WINEDEBUG": "-all",
        "WINEARCH": "win64",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name, value in environment.items():
        command += ["--setenv", name, value]
    return command + ["/runtime/.venv/bin/python", "-I", "/app/backend/wahlap_worker.py"]


@contextmanager
def _single_worker(state: Path):
    path = state / "worker.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WahlapError("busy") from None
            yield
    except OSError:
        raise WahlapError("runtime_unavailable") from None


def _kill_worker(process: subprocess.Popen):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def _run_worker(command: list[str], payload: dict, *, timeout: float = WORKER_TIMEOUT) -> dict:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except OSError:
        raise WahlapError("sandbox_unavailable") from None
    data = bytearray()
    deadline = time.monotonic() + timeout
    try:
        process.stdin.write(json.dumps(payload).encode())
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WahlapError("worker_timeout")
                for key, _ in selector.select(min(remaining, 1)):
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    data.extend(block)
                    if len(data) > MAX_OUTPUT_BYTES:
                        raise WahlapError("invalid_response")
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode != 0:
            raise WahlapError("worker_failed")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise WahlapError("invalid_response") from None
        if not isinstance(result, dict):
            raise WahlapError("invalid_response")
        if result.get("ok") is not True:
            raise _response_error(result)
        return result
    except subprocess.TimeoutExpired:
        raise WahlapError("worker_timeout") from None
    except (BrokenPipeError, OSError):
        raise WahlapError("worker_failed") from None
    finally:
        _kill_worker(process)
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()


def validate_result(result: dict) -> dict:
    """Revalidate the untrusted child-process output before a database caller sees it."""
    player_id = result.get("player_id")
    if not isinstance(player_id, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", player_id):
        raise WahlapError("invalid_response")
    records = result.get("records")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise WahlapError("invalid_scores")
    seen = set()
    clean = []
    for record in records:
        if not isinstance(record, dict):
            raise WahlapError("invalid_scores")
        required = ("song_id", "level_index", "achievements", "fc", "fs", "dxScore")
        if any(name not in record for name in required):
            raise WahlapError("incomplete_scores")
        song, level, dx = record["song_id"], record["level_index"], record["dxScore"]
        achievement = record["achievements"]
        if (
            type(song) is not int
            or not 0 < song < 10_000_000
            or type(level) is not int
            or not 0 <= level <= 5
            or type(dx) is not int
            or not 0 <= dx <= 100_000_000
            or type(achievement) not in (int, float)
            or not math.isfinite(achievement)
            or not 0 <= achievement <= (1000 if song >= 100000 else 101)
            or record["fc"] not in ("", "fc", "fcp", "ap", "app")
            or record["fs"] not in ("", "fs", "fsp", "fsd", "fsdp", "sync")
            or (song, level) in seen
        ):
            raise WahlapError("invalid_scores")
        seen.add((song, level))
        clean.append({name: record[name] for name in required})
    status = result.get("logout_status")
    # Confirmation requires the worker's post-logout preview, never merely HTTP 200.
    if (
        not isinstance(status, str)
        or status not in LOGOUT_NOTICES
        or result.get("logout_confirmed") is not (status == "confirmed")
    ):
        raise WahlapError("invalid_response")
    return {
        "records": clean,
        "player_id": player_id,
        "logout_confirmed": status == "confirmed",
        "logout_status": status,
        "notice": LOGOUT_NOTICES[status],
        "source": "wahlap",
    }


def _fetch_scores_local(sgid: str) -> dict:
    sgid = validate_sgid(sgid)
    runtime, state, wine, bwrap = _configured_paths()
    with _single_worker(state):
        result = _run_worker(_sandbox_command(runtime, state, wine, bwrap), {"sgid": sgid})
    return validate_result(result)


def fetch_scores(sgid: str) -> dict:
    if os.environ.get("MAIMAI_WAHLAP_ENABLED") != "1":
        raise WahlapError("disabled")
    sgid = validate_sgid(sgid)
    return validate_result(_socket_request({"kind": "fetch", "sgid": sgid}))
