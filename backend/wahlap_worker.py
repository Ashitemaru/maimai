"""One private QR input, one sanitized score response; run only inside bubblewrap."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import resource
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.wahlap import (  # noqa: E402
    MAX_RECORDS,
    WahlapError,
    validate_result,
    validate_sgid,
    verify_runtime,
)

MAX_PAGES = 400
PAGE_SIZE = 50
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


async def read_all_scores(client, player_id: int) -> list[dict]:
    cursor = 0
    cursors = set()
    keys = set()
    records = []
    for _ in range(MAX_PAGES):
        if cursor in cursors:
            raise WahlapError("pagination_invalid")
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
            raise WahlapError("pagination_invalid")
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
                    raise WahlapError("pagination_invalid")
        next_cursor = page.get("nextIndex")
        if type(next_cursor) is not int or not 0 <= next_cursor <= 10_000_000:
            raise WahlapError("pagination_invalid")
        if next_cursor == 0:
            return records
        if not groups or next_cursor <= cursor:
            raise WahlapError("pagination_invalid")
        cursor = next_cursor
    raise WahlapError("pagination_invalid")


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
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "maimai-gm.wahlap.com"
        or parsed.port != 42081
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise WahlapError("network_error")


async def collect_scores(sgid: str, modules) -> dict:
    owner = modules.client.MaimaiOfficialClient(timeout=20)
    try:
        session = await owner.resolve_session_async(sgid)
    except Exception:
        raise WahlapError("authorization_failed") from None
    player_id = _integer(session.user_id, 1, 4_294_967_295)
    endpoint = modules.protocol.DEFAULT_OFFICIAL_TITLE_ENDPOINTS[0]
    validate_endpoint(endpoint.base_url)
    settings = modules.protocol.OfficialTitleClient(
        base_url=endpoint.base_url,
        client_id=owner.client_id,
        timeout=15,
        host_header=endpoint.host_header,
        verify_tls=True,
    )
    client = settings
    accepted_login = None
    logout_status = "unverified"
    failure = None
    try:
        runtime_url = await settings.resolve_runtime_base_url(place_id=owner.place_id)
        validate_endpoint(runtime_url)
        if runtime_url != settings.base_url:
            client = settings.with_base_url(runtime_url)
        require_idle(await client.get_user_preview(session))
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
        records = await read_all_scores(client, player_id)
    except WahlapError as exc:
        failure = exc
    except Exception:
        failure = WahlapError("network_error")
    finally:
        if accepted_login is not None:
            logout_status = await end_session(client, session, accepted_login, owner)
        try:
            if client is not settings:
                await client.close()
        finally:
            await settings.close()
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
        if sys.argv[1:] in (["--probe"], ["--probe-runtime"]):
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
        result = {
            "ok": False,
            "error": exc.code,
            "logout_status": exc.logout_status,
            "logout_confirmed": exc.logout_confirmed,
        }
    except Exception:
        result = {"ok": False, "error": "worker_failed"}
    with os.fdopen(output_fd, "w") as response:
        json.dump(result, response, allow_nan=False, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
