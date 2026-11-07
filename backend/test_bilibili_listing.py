"""Handplay membership filtering, compact pagination and upstream protocol checks."""

from __future__ import annotations

import copy
import tempfile
import unittest
from unittest.mock import Mock

import httpx

from backend.bilibili import LISTING_TTL, BilibiliService
from backend.bilibili_client import (
    API,
    BilibiliClient,
    BilibiliError,
    CollectionUnavailableError,
)
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

    def test_rejected_collection_is_unavailable_but_transport_errors_are_not(self):
        def respond(request):
            if request.url.params["season_id"] == "11":
                return httpx.Response(200, json={"code": -404, "data": None})
            return httpx.Response(412)

        client = BilibiliClient(transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        with self.assertRaises(CollectionUnavailableError):
            client.collection_videos(11, ACCOUNT["mid"])
        with self.assertRaises(BilibiliError) as failure:
            client.collection_videos(12, ACCOUNT["mid"])
        self.assertNotIsInstance(failure.exception, CollectionUnavailableError)

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
        self.uploads = []
        self.client.videos = Mock(side_effect=self.upload_page)
        self.service = BilibiliService(FakeStore(temp.name), client_factory=self.client)
        self.service._save_session(ACCOUNT, self.client)
        self.addCleanup(self.service.close)

    def upload_page(self, page):
        start = (page - 1) * 20
        return {
            "items": copy.deepcopy(self.uploads[start : start + 20]),
            "page": page,
            "has_more": start + 20 < len(self.uploads),
        }

    def test_new_upload_is_listed_first_as_unorganized(self):
        self.uploads = [video(3, title="raw test upload"), video(2), video(1)]
        self.members[11] = [video(1)]
        self.members[13] = [video(2)]
        result = self.service.videos(1)
        self.assertEqual(
            [(v["aid"], v["organized"]) for v in result["items"]],
            [(3, False), (2, True), (1, True)],
        )
        self.assertFalse(result["has_more"])
        self.assertEqual(
            [call.args for call in self.client.collection_videos.call_args_list],
            [(11, ACCOUNT["mid"]), (12, ACCOUNT["mid"]), (13, ACCOUNT["mid"])],
        )

    def test_unorganized_before_organized_then_newest_first(self):
        self.uploads = [video(i) for i in range(25, 0, -1)]
        self.members[11] = [video(i) for i in range(1, 21)]
        self.members[12] = [video(i) for i in range(15, 21)]
        first, second, third = [self.service.videos(page) for page in (1, 2, 3)]
        self.assertEqual(
            [v["aid"] for v in first["items"]], [25, 24, 23, 22, 21, *range(20, 5, -1)]
        )
        self.assertTrue(first["has_more"])
        self.assertEqual([v["aid"] for v in second["items"]], [5, 4, 3, 2, 1])
        self.assertFalse(second["has_more"])
        self.assertEqual((third["items"], third["has_more"]), ([], False))
        self.assertEqual(self.client.videos.call_count, 2)
        self.assertEqual(self.client.collections.call_count, 1)
        self.assertEqual(len(first["collections"]), 5)

    def test_refresh_reads_uploads_and_membership_again(self):
        self.uploads = [video(1)]
        self.assertFalse(self.service.videos(1)["items"][0]["organized"])
        self.members[11] = [video(1)]
        self.assertTrue(self.service.videos(1)["items"][0]["organized"])
        self.uploads = []
        self.assertEqual(self.service.videos(1)["items"], [])

    def test_collection_read_failure_is_reported(self):
        self.uploads = [video(1)]
        self.client.collection_videos.side_effect = BilibiliError("upstream failed")
        with self.assertRaisesRegex(BilibiliError, "upstream failed"):
            self.service.videos(1)

    def test_unavailable_collection_is_reported_without_hiding_uploads(self):
        self.uploads = [video(2), video(1)]
        self.members[11] = [video(1)]

        def members(season_id, mid):
            if season_id == 12:
                raise CollectionUnavailableError("draft")
            return copy.deepcopy(self.members[season_id])

        self.client.collection_videos.side_effect = members
        result = self.service.videos(1)
        self.assertEqual(
            [(v["aid"], v["organized"]) for v in result["items"]], [(2, False), (1, True)]
        )
        self.assertEqual(result["unavailable_collections"], ["maimai 13+ AP"])

    def test_expired_listing_snapshot_is_reread_for_later_pages(self):
        self.uploads = [video(i) for i in range(25, 0, -1)]
        self.service.videos(1)
        self.service._listing_cache["at"] -= LISTING_TTL
        self.uploads = [video(i) for i in range(21, 0, -1)]
        self.assertEqual([v["aid"] for v in self.service.videos(2)["items"]], [1])
        self.assertEqual(self.client.collections.call_count, 2)

    def test_manual_bv_load_accepts_new_uncollected_owned_video(self):
        self.assertEqual(self.service.videos(1)["items"], [])
        self.assertEqual(self.service.video(BV)["bvid"], BV)
        self.assertEqual(self.client.calls, [])
