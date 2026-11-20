"""An in-memory Bilibili account and a connected service for the Bilibili tests."""

from __future__ import annotations

import base64
import copy
import io
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

from backend.bilibili import ApplyInput, BilibiliService, PrepareInput
from backend.bilibili_client import BilibiliError

BV = "BV1TbYo6xECK"
MIRROR_BV = "BV1bJouBSEiH"
ACCOUNT = {"mid": 1234, "name": "Test"}
TITLE = "【maimai】Example Master13.9 AP"


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
            {"filename": "original-video", "title": "Original part", "desc": "part", "cid": 789}
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


def preparation(**changes):
    body = PrepareInput(
        bvid=BV,
        chart_key="123:3",
        achievement="100.4567",
        combo="ap",
        constant="13.9",
        section_id=10,
    )
    return body.model_copy(update=changes)


class FakeStore:
    def __init__(self, path):
        self.runtime = Path(path)
        self.db_path = self.runtime / "personal.sqlite3"
        self.extra_charts = []
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
        return copy.deepcopy([self.chart_value, *self.extra_charts])


class FakeClient:
    """In-memory Bilibili account; ``fail`` names the step that times out."""

    def __init__(self):
        self.data = archive()
        self.mid = 1234
        self.calls = []
        self.fail = None
        self.collection_members = {10: [], 20: []}
        self.extra_videos = []
        self.edit_payloads = []
        self.delay_edit = False

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
        sections = [{"id": 10, "title": "13.9"}, {"id": 20, "title": "14.0"}]
        return {"items": [{"id": 1, "title": "Test collection", "sections": sections}]}

    def membership(self, data, sections):
        for key, episodes in self.collection_members.items():
            for episode in episodes:
                if episode["aid"] == 456:
                    return {"section_id": key, "episode_id": episode["id"]}
        return None

    def section(self, section_id):
        defaults = {"cid": 789, "bvid": self.data["archive"]["bvid"], "title": TITLE}
        return {
            "section": {"id": section_id, "seasonId": 1, "type": 0, "title": str(section_id)},
            "episodes": [defaults | copy.deepcopy(e) for e in self.collection_members[section_id]],
        }

    def videos(self, page):
        own = {"bvid": self.data["archive"]["bvid"], "title": self.data["archive"]["title"]}
        return {"items": [own, *self.extra_videos], "page": 1, "has_more": False}

    def step(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise BilibiliError(f"{name} timed out", uncertain=True)

    def sort_section(self, header, ids):
        self.calls.append("sort")
        rows = {episode["id"]: episode for episode in self.collection_members[header["id"]]}
        if self.fail != "sort_unapplied":
            self.collection_members[header["id"]] = [rows[key] for key in ids]
        if self.fail in ("sort", "sort_unapplied"):
            raise BilibiliError("sort timed out", uncertain=True)

    def upload_cover(self, value):
        self.step("cover")
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
        self.step("remove")
        for key in self.collection_members:
            self.collection_members[key] = []
        self.data["in_season"] = False

    def add_episode(self, section_id, data):
        self.step("add")
        self.collection_members[section_id].append({"aid": 456, "cid": 789, "id": 222})
        self.data["in_season"] = True


class ServiceCase(unittest.TestCase):
    """A connected service backed by ``FakeStore`` and ``FakeClient``."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = FakeStore(temp.name)
        self.client = FakeClient()
        self.service = self.restart()
        self.service._save_session(ACCOUNT, self.client)
        self.cover16 = png(1920)
        self.cover43 = png(1440)

    def restart(self):
        service = BilibiliService(self.store, client_factory=self.client)
        self.addCleanup(service.close)
        return service

    def prepare(self, **changes):
        return self.service.prepare(preparation(**changes))

    def apply(self, plan):
        body = ApplyInput(plan_id=plan["plan_id"], cover16=self.cover16, cover43=self.cover43)
        return self.service.apply(body)

    def retry(self, job):
        return self.service.retry(job["job_id"])
