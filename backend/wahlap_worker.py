"""One private QR input, one sanitized score response; run only inside bubblewrap."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import resource
import subprocess
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.wahlap import (  # noqa: E402
    MAX_RECORDS,
    WahlapError,
    error_response,
    validate_result,
    validate_sgid,
    verify_runtime,
)

MAX_PAGES = 400
PAGE_SIZE = 50
MAX_CURSOR = (1 << 64) - 1
FC = {0: "", 1: "fc", 2: "fcp", 3: "ap", 4: "app"}
FS = {0: "", 1: "fs", 2: "fsp", 3: "fsd", 4: "fsdp", 5: "sync"}


def _integer(value, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise WahlapError("invalid_scores")
    return value


def normalize_detail(detail: dict, outer_id=None) -> dict:
    """No missing-mark defaults: absence and an explicit zero are different evidence."""
    if not isinstance(detail, dict):
        raise WahlapError("invalid_scores")
    if any(name not in detail for name in ("level", "achievement", "comboStatus", "syncStatus")):
        raise WahlapError("incomplete_scores")
    music_id = _integer(detail.get("musicId", outer_id), 1, 9_999_999)
    if outer_id is not None and _integer(outer_id, 1, 9_999_999) != music_id:
        raise WahlapError("invalid_scores")
    level = _integer(detail["level"], 0, 5)
    achievement = _integer(
        detail["achievement"], 0, 10_000_000 if music_id >= 100000 else 1_010_000
    )
    combo = _integer(detail["comboStatus"], 0, 4)
    sync = _integer(detail["syncStatus"], 0, 5)
    dx_fields = ("deluxscoreMax", "deluxeScoreMax", "deluxScoreMax", "dxScore", "dx_score")
    values = [detail[name] for name in dx_fields if name in detail]
    if not values:
        raise WahlapError("incomplete_scores")
    dx_score = _integer(values[0], 0, 100_000_000)
    if any(type(value) is not int or value != dx_score for value in values):
        raise WahlapError("invalid_scores")
    return {
        "song_id": music_id,
        "level_index": level,
        "achievements": achievement / 10000,
        "fc": FC[combo],
        "fs": FS[sync],
        "dxScore": dx_score,
    }


def _preview_idle(preview: object) -> bool | None:
    if not isinstance(preview, dict) or type(preview.get("errorId")) is not int:
        return None
    if preview["errorId"] != 0:
        return None
    state = preview.get("isLogin")
    if state is False or type(state) is int and state == 0:
        return True
    if state is True or type(state) is int and state == 1:
        return False
    return None


def require_idle(preview: object):
    idle = _preview_idle(preview)
    if idle is False:
        raise WahlapError("session_active")
    if idle is None:
        raise WahlapError("session_state_unknown")


def pagination_error(reason: str) -> WahlapError:
    return WahlapError("pagination_invalid", pagination_reason=reason)


def pagination_cursor(value: object) -> int:
    """Echo an opaque server cursor; it is not a bounded or increasing row offset."""
    if isinstance(value, str):
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value):
            raise pagination_error("cursor_type")
        value = int(value)
    if type(value) is not int:
        raise pagination_error("cursor_type")
    if not 0 <= value <= MAX_CURSOR:
        raise pagination_error("cursor_range")
    return value


async def read_all_scores(client, player_id: int) -> list[dict]:
    cursor = 0
    cursors = set()
    keys = set()
    records = []
    for _ in range(MAX_PAGES):
        if cursor in cursors:
            raise pagination_error("cursor_cycle")
        cursors.add(cursor)
        page = await client.post(
            "GetUserMusicApi",
            player_id,
            {"userId": player_id, "nextIndex": cursor, "maxCount": PAGE_SIZE},
        )
        if not isinstance(page, dict) or not isinstance(page.get("userMusicList"), list):
            raise WahlapError("incomplete_scores")
        if "userId" in page and page["userId"] != player_id:
            raise WahlapError("invalid_scores")
        groups = page["userMusicList"]
        if len(groups) > PAGE_SIZE:
            raise pagination_error("group_limit")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(
                group.get("userMusicDetailList"), list
            ):
                raise WahlapError("incomplete_scores")
            details = group["userMusicDetailList"]
            if not details or len(details) > 6:
                raise WahlapError("invalid_scores")
            for detail in details:
                record = normalize_detail(detail, group.get("musicId"))
                key = (record["song_id"], record["level_index"])
                if key in keys:
                    raise WahlapError("invalid_scores")
                keys.add(key)
                records.append(record)
                if len(records) > MAX_RECORDS:
                    raise pagination_error("record_limit")
        next_cursor = pagination_cursor(page.get("nextIndex"))
        if next_cursor == 0:
            return records
        cursor = next_cursor
    raise pagination_error("page_limit")


async def end_session(client, session, login: dict, owner) -> str:
    try:
        response = await client.post(
            "UserLogoutApi",
            session.user_id,
            {
                "userId": session.user_id,
                "accessCode": "",
                "regionId": owner.region_id,
                "placeId": owner.place_id,
                "clientId": owner.cabinet_client_id,
                "loginDateTime": login["_loginDateTime"],
                "type": 5,
            },
        )
        if not isinstance(response, dict):
            return "failed"
        # The logout returnCode is undocumented. Confirm state with a fresh read instead.
        preview = await client.get_user_preview(session)
        return "confirmed" if _preview_idle(preview) is True else "unverified"
    except Exception:
        return "failed"


def validate_endpoint(url: str):
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        allowed = parsed is not None and (
            parsed.scheme == "https"
            and parsed.hostname == "maimai-gm.wahlap.com"
            and parsed.port == 42081
            and not any((parsed.username, parsed.password, parsed.query, parsed.fragment))
        )
    except ValueError:
        allowed = False
    if not allowed:
        raise WahlapError("invalid_endpoint")


def classify_failure(exc: Exception, protocol, stage: str) -> WahlapError:
    """Export only fixed categories, never URLs, exception bodies or response payloads."""
    if isinstance(exc, WahlapError):
        if exc.stage is None:
            exc.stage = stage
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        return WahlapError("upstream_http_error", stage=stage, http_status=exc.response.status_code)
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return WahlapError("upstream_timeout", stage=stage)
    if isinstance(exc, httpx.TransportError):
        return WahlapError("upstream_transport_error", stage=stage)
    server_error = getattr(protocol, "OfficialTitleServerError", None)
    if isinstance(server_error, type) and isinstance(exc, server_error):
        # These exact markers are part of our hash-pinned upstream protocol adapter.
        code = {
            "empty official title response": "upstream_empty_response",
            "invalid official title response": "upstream_decode_error",
        }.get(str(exc))
        if code:
            return WahlapError(code, stage=stage)
    return WahlapError("worker_failed", stage=stage)


@asynccontextmanager
async def configured_client(modules):
    """Resolve the account-free configuration before any QR or login operation."""
    settings = client = None
    try:
        try:
            owner = modules.client.MaimaiOfficialClient(timeout=20)
            endpoint = modules.protocol.DEFAULT_OFFICIAL_TITLE_ENDPOINTS[0]
            validate_endpoint(endpoint.base_url)
            settings = modules.protocol.OfficialTitleClient(
                base_url=endpoint.base_url,
                client_id=owner.client_id,
                timeout=15,
                host_header=endpoint.host_header,
                verify_tls=True,
            )
            response = await settings.get_game_setting(place_id=owner.place_id)
            if not isinstance(response, dict):
                raise WahlapError("upstream_decode_error")
            game_setting = response.get("gameSetting")
            if not isinstance(game_setting, dict) or not game_setting:
                raise WahlapError("upstream_decode_error")
            runtime_path = game_setting.get("movieServerUri")
            if runtime_path is not None and not isinstance(runtime_path, str):
                raise WahlapError("upstream_decode_error")
            runtime_path = (runtime_path or "").strip()
            if not runtime_path:
                runtime_path = modules.protocol.DEFAULT_RUNTIME_SERVER_URI
            runtime_url = urljoin(settings.base_url, runtime_path)
            validate_endpoint(runtime_url)
            client = (
                settings
                if runtime_url == settings.base_url
                else settings.with_base_url(runtime_url)
            )
        except Exception as exc:
            failure = classify_failure(exc, modules.protocol, "configuration")
            failure.logout_status = "not_attempted"
            raise failure from None
        yield owner, client
    finally:
        # A close failure must not erase the original error or confirmed logout evidence.
        if client is not None and client is not settings:
            with suppress(Exception):
                await client.close()
        if settings is not None:
            with suppress(Exception):
                await settings.close()


async def probe_network(modules) -> dict:
    async with configured_client(modules):
        return {"ok": True, "probe": "network", "account_access": False}


async def collect_scores(sgid: str, modules) -> dict:
    async with configured_client(modules) as (owner, client):
        return await _collect_account_scores(sgid, modules, owner, client)


async def _collect_account_scores(sgid: str, modules, owner, client) -> dict:
    accepted_login = None
    logout_status = "unverified"
    failure = None
    stage = "authorization"
    try:
        try:
            session = await owner.resolve_session_async(sgid)
        except Exception:
            raise WahlapError("authorization_failed") from None
        player_id = _integer(session.user_id, 1, 4_294_967_295)
        stage = "preview"
        require_idle(await client.get_user_preview(session))
        stage = "login"
        try:
            login = await client.user_login(
                session,
                region_id=owner.region_id,
                place_id=owner.place_id,
                client_id=owner.cabinet_client_id,
            )
        except Exception:
            raise WahlapError("login_state_unknown") from None
        if (
            not isinstance(login, dict)
            or type(login.get("returnCode")) is not int
            or login["returnCode"] != 1
        ):
            # In particular, do not accept code 100 or logout an existing session.
            raise WahlapError("login_failed")
        if type(login.get("_loginDateTime")) is not int or login["_loginDateTime"] <= 0:
            raise WahlapError("login_state_unknown")
        accepted_login = login
        stage = "scores"
        records = await read_all_scores(client, player_id)
    except Exception as exc:
        failure = classify_failure(exc, modules.protocol, stage)
    finally:
        if accepted_login is not None:
            logout_status = await end_session(client, session, accepted_login, owner)
    if failure is not None:
        failure.logout_status = logout_status if accepted_login else "not_attempted"
        failure.logout_confirmed = logout_status == "confirmed"
        raise failure from None
    return validate_result(
        {
            "records": records,
            "player_id": str(player_id),
            "logout_status": logout_status,
            "logout_confirmed": logout_status == "confirmed",
        }
    )


def _load_upstream(runtime: Path):
    verify_runtime(runtime)
    sys.path.insert(0, str(runtime / "upstream"))
    client = importlib.import_module("maimai_official_interface.client")
    protocol = importlib.import_module("maimai_official_interface.protocol")
    bridge = importlib.import_module("maimai_official_interface.wine_bridge")

    def contained_run(*args, **kwargs):
        # Keep Wine in the worker PID namespace/process group so timeout cleanup includes it.
        kwargs["start_new_session"] = False
        return subprocess.run(*args, **kwargs)

    bridge.subprocess = SimpleNamespace(
        run=contained_run, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired
    )
    return SimpleNamespace(client=client, protocol=protocol, bridge=bridge)


def _limits():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (150, 160))
    # Wine maps a large virtual address space; RLIMIT_AS would break its startup.
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))


def main() -> int:
    # Upstream code or native DLL diagnostics cannot share the JSON response stream.
    output_fd = os.dup(sys.stdout.fileno())
    with open(os.devnull, "w") as quiet:
        os.dup2(quiet.fileno(), sys.stdout.fileno())
        os.dup2(quiet.fileno(), sys.stderr.fileno())
    try:
        _limits()
        runtime = Path(os.environ["MAIMAI_WAHLAP_RUNTIME_DIR"])
        state = Path(os.environ["MAIMAI_WAHLAP_STATE_DIR"])
        (state / "home").mkdir(mode=0o700, exist_ok=True)
        modules = _load_upstream(runtime)
        if sys.argv[1:] == ["--probe-network"]:
            result = asyncio.run(probe_network(modules))
        elif sys.argv[1:] in (["--probe"], ["--probe-runtime"]):
            importlib.import_module("httpx")
            importlib.import_module("cryptography")
            result = {"ok": True, "probe": "imports", "account_access": False}
            if sys.argv[1:] == ["--probe-runtime"]:
                resolver = modules.bridge.WineChimeBridge(
                    dll_path=runtime / "upstream/maimai_official_interface/resources/core.dat",
                    timeout=40,
                )
                result["probe"] = resolver.probe()
        else:
            raw = sys.stdin.buffer.read(4097)
            if len(raw) > 4096:
                raise WahlapError("invalid_qr")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise WahlapError("invalid_qr")
            sgid = validate_sgid(request.get("sgid"))
            result = {"ok": True, **asyncio.run(collect_scores(sgid, modules))}
    except WahlapError as exc:
        result = error_response(exc)
    except Exception:
        result = {"ok": False, "error": "worker_failed"}
    with os.fdopen(output_fd, "w") as response:
        json.dump(result, response, allow_nan=False, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
