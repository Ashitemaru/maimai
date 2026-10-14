"""Small, bounded Bilibili web client; no mutating request is retried here.

Protocol references: biliup's uploader/bilibili.rs and uploader/season.rs, plus
our successful 2026-10-01 web submissions under data/bilibili-upload-v8.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from urllib.parse import urlsplit

import httpx

MEMBER = "https://member.bilibili.com"
API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
QR_LOGIN_PAGES = {
    ("account.bilibili.com", "/h5/account-h5/auth/scan-web"),
    ("passport.bilibili.com", "/h5-app/passport/login/scan"),
}
BVID = re.compile(r"BV[0-9A-Za-z]{10}\Z")
MAX_RESPONSE = 4 * 1024 * 1024


class BilibiliError(Exception):
    def __init__(self, message, *, status=502, uncertain=False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


def validate_bvid(value):
    if not isinstance(value, str) or not BVID.fullmatch(value):
        raise BilibiliError("请输入完整 BV 号", status=422)
    return value


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def cover_identity(value):
    """Bilibili can return http/https or protocol-relative forms of the same URL."""
    parsed = urlsplit("https:" + value if str(value).startswith("//") else str(value))
    host = parsed.hostname or ""
    if any(host == d or host.endswith("." + d) for d in ("hdslb.com", "biliimg.com")):
        return ("bilibili-cdn", parsed.path)
    return parsed.netloc.lower(), parsed.path


def edit_payload(data):
    """Preserve the fields the real web editor submitted, never uploader defaults.

    Rich special-purpose submissions need their dedicated editor. Reject them
    before any write instead of silently dropping collaboration/payment state.
    """
    archive = data.get("archive", {})
    videos = data.get("videos")
    required = {"aid", "mid", "bvid", "title", "copyright", "tid", "tag", "desc"}
    if not required.issubset(archive) or not videos:
        raise BilibiliError("B 站未返回完整稿件信息，无法安全修改")
    for field in (
        "interactive",
        "ugcpay",
        "is_ugcpay_v2",
        "charging_pay",
        "is_playlet",
        "upower_mode",
        "is_pugv",
        "is_staff",
        "is_ab_cover",
        "premiere",
    ):
        if archive.get(field):
            raise BilibiliError("该稿件包含特殊投稿设置，请在 B 站编辑", status=409)
    if data.get("staffs") or archive.get("staffs") or archive.get("dtime", 0):
        raise BilibiliError("合作或定时稿件请在 B 站编辑", status=409)
    fields = (
        "aid",
        "title",
        "cover",
        "cover43",
        "copyright",
        "source",
        "tid",
        "tid_v2",
        "tag",
        "desc",
        "desc_v2",
        "desc_format_id",
        "dynamic",
        "dynamic_v2",
        "ai_cover",
        "is_ab_cover",
        "ab_cover_info",
        "creation_statement",
        "human_type2",
        "recreate",
        "interactive",
        "mission_id",
        "is_only_self",
        "no_reprint",
        "is_360",
        "lossless_music",
        "topic_id",
        "topic_name",
        "neutral_mark",
    )
    body = {key: copy.deepcopy(archive[key]) for key in fields if key in archive}
    for key, member in (("human_type2", "id"), ("recreate", "switch")):
        if isinstance(body.get(key), dict):
            body[key] = body[key][member]
    if isinstance(body.get("creation_statement"), dict):
        body["creation_statement"] = {"id": body["creation_statement"]["id"]}
    if not body.get("is_ab_cover"):
        body["ab_cover_info"] = None
    body.update(new_web_edit=1, handle_staff=False, topic_grey=1, web_os=2)
    if "is_dolby" in archive:
        body["dolby"] = archive["is_dolby"]
    if "space_hidden" in data:
        body["space_hidden"] = data["space_hidden"]
    if "watermark" in data:
        body["watermark"] = {"state": data["watermark"]["state"]}
    if "subtitle" in data:
        subtitle = data["subtitle"]
        body["subtitle"] = {
            "open": int(bool(subtitle.get("allow"))),
            "lan": subtitle.get("lan", ""),
        }
    reply = data.get("reply", {})
    if reply.get("state") or reply.get("up_selection"):
        raise BilibiliError("该稿件包含特殊评论设置，请在 B 站编辑", status=409)
    body["videos"] = []
    for video in videos:
        if not all(key in video for key in ("filename", "title", "desc", "cid")):
            raise BilibiliError("B 站未返回完整分 P 信息，无法安全修改")
        body["videos"].append({key: video[key] for key in ("filename", "title", "desc", "cid")})
    return body


def archive_revision(data):
    return digest(edit_payload(data))


def collection_video(row):
    if not isinstance(row, dict):
        raise BilibiliError("B 站合集稿件格式已变化")
    bvid, aid, title = row.get("bvid"), row.get("aid"), row.get("title")
    created_at = row.get("pubdate") or row.get("ctime")
    if (
        not isinstance(bvid, str)
        or not BVID.fullmatch(bvid)
        or type(aid) is not int
        or aid <= 0
        or not isinstance(title, str)
        or not title
        or type(created_at) is not int
        or created_at < 0
        or not isinstance(row.get("pic", ""), str)
    ):
        raise BilibiliError("B 站合集稿件信息不完整")
    return {
        "bvid": bvid,
        "aid": aid,
        "title": title,
        "cover": row.get("pic", ""),
        "created_at": created_at,
    }


class BilibiliClient:
    def __init__(self, cookies=(), *, transport=None):
        self.http = httpx.Client(
            timeout=httpx.Timeout(25, connect=8),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
            headers={"User-Agent": "Mozilla/5.0", "Referer": MEMBER + "/", "Origin": MEMBER},
        )
        for cookie in cookies:
            domain = str(cookie.get("domain", ""))
            if domain not in (".bilibili.com", "bilibili.com", "passport.bilibili.com"):
                continue
            self.http.cookies.set(cookie["name"], cookie["value"], domain=domain, path="/")

    def close(self):
        self.http.close()

    def cookies(self):
        return [
            {"name": c.name, "value": c.value, "domain": c.domain}
            for c in self.http.cookies.jar
            if c.domain.lstrip(".") in ("bilibili.com", "passport.bilibili.com")
        ]

    def csrf(self):
        for cookie in self.http.cookies.jar:
            if cookie.name == "bili_jct" and cookie.domain.lstrip(".") == "bilibili.com":
                return cookie.value
        raise BilibiliError("B 站登录已失效，请重新扫码", status=409)

    def request(self, method, host, path, **kwargs):
        if host not in (MEMBER, API, PASSPORT):
            raise BilibiliError("无效的 B 站请求地址")
        mutation = method != "GET"
        try:
            with self.http.stream(method, host + path, **kwargs) as response:
                if response.status_code in (401, 403):
                    raise BilibiliError("B 站登录失效或请求被限制，请重新登录后再试", status=409)
                if response.status_code != 200:
                    raise BilibiliError("B 站暂时无法响应，请稍后重试", uncertain=mutation)
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise BilibiliError("B 站响应过大，已停止处理", uncertain=mutation)
                result = json.loads(content)
        except (httpx.HTTPError, ValueError, TypeError):
            # URLs, upstream messages and response bodies can contain credentials.
            raise BilibiliError("B 站请求未完成，请稍后重试", uncertain=mutation) from None
        if not isinstance(result, dict) or not isinstance(result.get("code"), int):
            raise BilibiliError("B 站返回了无法识别的结果", uncertain=mutation)
        if result["code"] in (-101, -111):
            raise BilibiliError("B 站登录已失效，请重新扫码", status=409)
        if result["code"] != 0:
            raise BilibiliError(f"B 站未接受请求（{result['code']}）", status=409)
        return result.get("data") or {}

    def account(self):
        data = self.request("GET", API, "/x/web-interface/nav")
        if not data.get("isLogin") or not data.get("mid"):
            raise BilibiliError("请先登录 B 站", status=409)
        return {"mid": int(data["mid"]), "name": str(data.get("uname", ""))}

    def start_login(self):
        data = self.request("GET", PASSPORT, "/x/passport-login/web/qrcode/generate")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("url"), str)
            or not isinstance(data.get("qrcode_key"), str)
            or not data["qrcode_key"].strip()
        ):
            raise BilibiliError("B 站没有返回有效登录二维码")
        raw_url = data["url"]
        try:
            url = urlsplit(raw_url)
            valid_url = (
                url.scheme == "https"
                and (url.hostname, url.path) in QR_LOGIN_PAGES
                and url.port in (None, 443)
                and url.username is None
                and url.password is None
                and not url.fragment
                and raw_url == raw_url.strip()
                and not any(ord(character) < 32 for character in raw_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise BilibiliError("B 站没有返回有效登录二维码")
        return data

    def poll_login(self, key):
        return self.request(
            "GET", PASSPORT, "/x/passport-login/web/qrcode/poll", params={"qrcode_key": key}
        )

    def videos(self, page):
        data = self.request(
            "GET",
            MEMBER,
            "/x/web/archives",
            params={"status": "is_pubing,pubed,not_pubed", "pn": page, "ps": 20},
        )
        raw = data.get("arc_audits") or []
        items = []
        for row in raw:
            archive = row.get("Archive") or row.get("archive")
            if not isinstance(archive, dict):
                raise BilibiliError("B 站稿件列表格式已变化")
            items.append(
                {
                    "bvid": archive["bvid"],
                    "aid": archive["aid"],
                    "title": archive["title"],
                    "cover": archive.get("cover", ""),
                    "created_at": archive.get("ctime"),
                }
            )
        paging = data.get("page", {})
        size = int(paging.get("ps") or 20)
        total = int(paging.get("count") or paging.get("total") or 0)
        return {"items": items, "page": page, "has_more": page * size < total}

    def archive(self, bvid, mid):
        data = self.request(
            "GET",
            MEMBER,
            "/x/vupre/web/archive/view",
            params={"topic_grey": 1, "bvid": validate_bvid(bvid)},
        )
        archive = data.get("archive", {})
        if archive.get("mid") != mid or archive.get("bvid") != bvid:
            raise BilibiliError("该稿件不属于当前登录账号", status=409)
        edit_payload(data)
        return data

    def collection_videos(self, season_id, mid):
        """Read a complete collection, without loading every video's edit payload."""
        items = []
        seen = set()
        expected_total = None
        for page in range(1, 101):
            data = self.request(
                "GET",
                API,
                "/x/polymer/web-space/seasons_archives_list",
                params={
                    "mid": mid,
                    "season_id": season_id,
                    "sort_reverse": "false",
                    "page_num": page,
                    "page_size": 100,
                },
            )
            if not isinstance(data, dict):
                raise BilibiliError("B 站合集稿件格式已变化")
            meta, paging = data.get("meta"), data.get("page")
            if not isinstance(meta, dict) or not isinstance(paging, dict):
                raise BilibiliError("B 站未返回完整合集信息")
            if meta.get("mid") != mid or meta.get("season_id") != season_id:
                raise BilibiliError("B 站返回的合集与当前账号不一致")
            size, total = paging.get("page_size"), paging.get("total")
            if (
                paging.get("page_num") != page
                or type(size) is not int
                or not 1 <= size <= 100
                or type(total) is not int
                or not 0 <= total <= 10000
                or meta.get("total") != total
            ):
                raise BilibiliError("B 站合集分页信息不完整")
            if expected_total is not None and expected_total != total:
                raise BilibiliError("B 站合集内容发生变化，请刷新后重试")
            expected_total = total
            rows = data.get("archives")
            if not isinstance(rows, list) or len(rows) != min(size, total - len(items)):
                raise BilibiliError("B 站未返回完整合集稿件，请刷新后重试")
            for row in rows:
                item = collection_video(row)
                if item["bvid"] in seen:
                    raise BilibiliError("B 站合集分页出现重复稿件，请刷新后重试")
                seen.add(item["bvid"])
                items.append(item)
            if len(items) == total:
                return items
        raise BilibiliError("合集稿件数量超出处理上限", status=409)

    def collections(self):
        items = []
        for page in range(1, 11):
            data = self.request(
                "GET",
                MEMBER,
                "/x2/creative/web/seasons",
                params={"pn": page, "ps": 30, "order": "mtime", "sort": "desc", "draft": "1"},
            )
            rows = data.get("seasons") or []
            for row in rows:
                season = row["season"]
                sections = (row.get("sections") or {}).get("sections") or []
                items.append(
                    {
                        "id": int(season["id"]),
                        "title": season["title"],
                        "sections": [{"id": int(s["id"]), "title": s["title"]} for s in sections],
                    }
                )
            if len(rows) < 30:
                return {"items": items}
        raise BilibiliError("合集数量超出处理上限，请先在 B 站管理", status=409)

    def section(self, section_id):
        return self.request(
            "GET", MEMBER, "/x2/creative/web/season/section", params={"id": section_id}
        )

    def membership(self, data, allowed_sections):
        if not data.get("in_season"):
            return None
        archive = data["archive"]
        view = self.request("GET", API, "/x/web-interface/view", params={"bvid": archive["bvid"]})
        for section in (view.get("ugc_season") or {}).get("sections") or []:
            if section.get("id") in allowed_sections:
                detail = self.section(section["id"])
                for episode in detail.get("episodes") or []:
                    if episode.get("aid") == archive["aid"]:
                        return {"section_id": section["id"], "episode_id": episode["id"]}
        raise BilibiliError("无法确认稿件当前合集，请在 B 站检查后再试", status=409)

    def upload_cover(self, data_url):
        data = self.request(
            "POST",
            MEMBER,
            "/x/vu/web/cover/up",
            files={"cover": (None, data_url), "csrf": (None, self.csrf())},
        )
        url = data.get("url", "")
        parsed = urlsplit("https:" + url if url.startswith("//") else url)
        host = parsed.hostname or ""
        owned_cdn = any(
            host == domain or host.endswith("." + domain) for domain in ("hdslb.com", "biliimg.com")
        )
        if parsed.scheme not in ("https", "http") or not owned_cdn:
            raise BilibiliError("B 站未返回有效封面地址", uncertain=True)
        return url

    def edit(self, body):
        return self.request(
            "POST", MEMBER, "/x/vu/web/edit", params={"csrf": self.csrf()}, json=body
        )

    def remove_episode(self, episode_id):
        return self.request(
            "POST",
            MEMBER,
            "/x2/creative/web/season/section/episode/del",
            data={"id": episode_id, "csrf": self.csrf()},
        )

    def add_episode(self, section_id, archive):
        video = archive["videos"][0]
        return self.request(
            "POST",
            MEMBER,
            "/x2/creative/web/season/section/episodes/add",
            params={"csrf": self.csrf()},
            json={
                "sectionId": section_id,
                "episodes": [
                    {
                        "aid": archive["archive"]["aid"],
                        "cid": video["cid"],
                        "title": archive["archive"]["title"],
                        "charging_pay": 0,
                    }
                ],
                "csrf": self.csrf(),
            },
        )

    def sort_section(self, header, episode_ids):
        if len(episode_ids) != len(set(episode_ids)):
            raise BilibiliError("合集排序存在重复稿件", status=409)
        return self.request(
            "POST",
            MEMBER,
            "/x2/creative/web/season/section/edit",
            params={"csrf": self.csrf()},
            json={
                "section": {key: header[key] for key in ("id", "type", "seasonId", "title")},
                "sorts": [
                    {"id": episode_id, "sort": i + 1} for i, episode_id in enumerate(episode_ids)
                ],
                "captcha_token": "",
            },
        )
