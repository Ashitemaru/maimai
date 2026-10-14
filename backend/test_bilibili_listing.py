"""Handplay membership filtering, compact pagination and upstream protocol checks."""

from __future__ import annotations

import copy
import tempfile
import unittest
from unittest.mock import Mock

import httpx

from backend.bilibili import BilibiliService
from backend.bilibili_client import API, BilibiliClient, BilibiliError
from backend.test_bilibili import ACCOUNT, BV, FakeClient, FakeStore


def video(index, **changes):
    return {
        "bvid": f"BV{index:010d}",
        "aid": index,
        "title": f"Handplay {index}",
        "cover": "https://i1.hdslb.com/bfs/archive/test.jpg",
        "created_at": 1000 + index,
        **changes,
    }


def archive_row(index):
    item = video(index)
    item["pic"] = item.pop("cover")
    item["pubdate"] = item.pop("created_at")
    item["ctime"] = item["pubdate"] - 100
    return item


def collection_page(page=1, total=1, size=100):
    return {
        "meta": {"mid": ACCOUNT["mid"], "season_id": 11, "total": total},
        "page": {"page_num": page, "page_size": size, "total": total},
        "archives": [archive_row(i + 1) for i in range((page - 1) * size, min(page * size, total))],
    }


class CollectionProtocolTests(unittest.TestCase):
    def client(self, responses):
        self.requests = []

        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": responses[len(self.requests) - 1]})

        client = BilibiliClient(transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        return client

    def test_reads_all_pages_and_maps_public_archive_fields(self):
        client = self.client([collection_page(total=101), collection_page(page=2, total=101)])
        items = client.collection_videos(11, ACCOUNT["mid"])
        self.assertEqual(items, [video(i) for i in range(1, 102)])
        for number, request in enumerate(self.requests, 1):
            self.assertEqual(request.method, "GET")
            self.assertEqual(
                str(request.url).split("?")[0], API + "/x/polymer/web-space/seasons_archives_list"
            )
            self.assertEqual(request.url.params["mid"], str(ACCOUNT["mid"]))
            self.assertEqual(request.url.params["season_id"], "11")
            self.assertEqual(request.url.params["page_num"], str(number))
            self.assertEqual(request.url.params["page_size"], "100")
            self.assertEqual(request.url.params["sort_reverse"], "false")

    def test_empty_collection(self):
        self.assertEqual(
            self.client([collection_page(total=0)]).collection_videos(11, ACCOUNT["mid"]), []
        )

    def test_respects_upstream_page_size(self):
        client = self.client(
            [collection_page(total=3, size=2), collection_page(page=2, total=3, size=2)]
        )
        self.assertEqual(len(client.collection_videos(11, ACCOUNT["mid"])), 3)

    def test_rejects_wrong_owner_or_collection(self):
        for field in ("mid", "season_id"):
            with self.subTest(field=field):
                data = collection_page()
                data["meta"][field] = 999
                with self.assertRaises(BilibiliError):
                    self.client([data]).collection_videos(11, ACCOUNT["mid"])

    def test_rejects_incomplete_or_malformed_responses(self):
        cases = [[], {}, collection_page(), collection_page(), collection_page(), collection_page()]
        cases[2]["archives"] = []
        cases[3]["page"]["page_num"] = 2
        cases[4]["page"]["page_size"] = 0
        cases[5]["archives"][0]["bvid"] = "invalid"
        for data in cases:
            with self.subTest(data=data), self.assertRaises(BilibiliError):
                self.client([data]).collection_videos(11, ACCOUNT["mid"])

    def test_rejects_collection_changed_between_pages(self):
        client = self.client([collection_page(total=101), collection_page(page=2, total=102)])
        with self.assertRaisesRegex(BilibiliError, "发生变化"):
            client.collection_videos(11, ACCOUNT["mid"])

    def test_rejects_duplicate_page_instead_of_silently_losing_members(self):
        second = collection_page(page=2, total=101)
        second["archives"] = [archive_row(1)]
        with self.assertRaisesRegex(BilibiliError, "重复"):
            self.client([collection_page(total=101), second]).collection_videos(11, ACCOUNT["mid"])


class HandplayListingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.client = FakeClient()
        self.client.collections = Mock(
            return_value={
                "items": [
                    {"id": 11, "title": "maimai 13 AP"},
                    {"id": 12, "title": "maimai 13+ AP"},
                    {"id": 13, "title": "ＭＡＩＭＡＩ １４ ＡＰ"},
                    {"id": 14, "title": "Other games"},
                    {"id": 15, "title": "maimai 13 AP score screenshots"},
                ]
            }
        )
        self.members = {11: [], 12: [], 13: []}
        self.client.collection_videos = Mock(
            side_effect=lambda season_id, mid: copy.deepcopy(self.members[season_id])
        )
        self.client.videos = Mock(side_effect=AssertionError("Do not list all account uploads"))
        self.service = BilibiliService(FakeStore(temp.name), client_factory=self.client)
        self.service._save_session(ACCOUNT, self.client)
        self.addCleanup(self.service.close)

    def test_only_collection_members_regardless_of_video_title(self):
        self.members[11] = [video(1, bvid="BV1bJouBSEiH", title="PA reversed title")]
        self.members[12] = [video(2, bvid="BV1mf796VEhd", title="【maimai】宴 AP+")]
        self.members[13] = [video(3, bvid=BV)]
        result = self.service.videos(1)
        self.assertEqual([v["bvid"] for v in result["items"]], [BV, "BV1mf796VEhd", "BV1bJouBSEiH"])
        self.assertFalse(result["has_more"])
        self.assertEqual(
            [call.args for call in self.client.collection_videos.call_args_list],
            [(11, ACCOUNT["mid"]), (12, ACCOUNT["mid"]), (13, ACCOUNT["mid"])],
        )
        self.assertEqual(self.client.calls, [])

    def test_deduplicates_before_pagination_and_sorts_newest_first(self):
        self.members[11] = [video(i) for i in range(1, 16)]
        self.members[12] = [video(i) for i in range(10, 26)]
        first, second, third = [self.service.videos(page) for page in (1, 2, 3)]
        self.assertEqual([v["aid"] for v in first["items"]], list(range(25, 5, -1)))
        self.assertTrue(first["has_more"])
        self.assertEqual([v["aid"] for v in second["items"]], [5, 4, 3, 2, 1])
        self.assertFalse(second["has_more"])
        self.assertEqual(third, {"items": [], "has_more": False, "page": 3})

    def test_refresh_reads_membership_again_and_handles_empty_collections(self):
        self.members[11] = [video(1)]
        self.assertEqual(len(self.service.videos(1)["items"]), 1)
        self.members[11] = []
        self.assertEqual(self.service.videos(1), {"items": [], "has_more": False, "page": 1})
        self.client.collections.return_value = {"items": []}
        self.assertEqual(self.service.videos(1)["items"], [])

    def test_incomplete_collection_does_not_fall_back_to_all_uploads(self):
        self.client.collection_videos.side_effect = BilibiliError("upstream failed")
        with self.assertRaisesRegex(BilibiliError, "upstream failed"):
            self.service.videos(1)
        self.client.videos.assert_not_called()

    def test_manual_bv_load_accepts_new_uncollected_owned_video(self):
        self.assertEqual(self.service.videos(1)["items"], [])
        self.assertEqual(self.service.video(BV)["bvid"], BV)
        self.assertEqual(self.client.calls, [])
