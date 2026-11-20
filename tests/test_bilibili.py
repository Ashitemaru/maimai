"""Bilibili video titles, stale-title detection, and read-back-verified writes."""

import unittest

from backend.bilibili import chart_ids, has_both_types, title_for
from backend.bilibili_order import title_combo
from tests.fixtures.bilibili import BV, MIRROR_BV, TITLE, ServiceCase, archive, preparation


def episode(aid, song):
    title = f"【maimai】{song} Master13.9 AP"
    return {
        "id": aid + 1000,
        "aid": aid,
        "cid": aid + 2000,
        "bvid": f"BV{aid:010d}",
        "title": title,
    }


def chart(key, song, version):
    return {
        "chart_key": key,
        "title": song,
        "type": "standard",
        "difficulty_index": 3,
        "jp_release_version": version,
    }


class TitleTests(unittest.TestCase):
    def test_title_carries_type_suffix_difficulty_constant_and_combo(self):
        standard = {"chart_key": "5:3", "title": "Song", "difficulty_index": 3, "type": "standard"}
        dx = standard | {"chart_key": "10005:3", "type": "dx"}
        both = chart_ids([standard, dx])
        self.assertTrue(has_both_types(both, standard))
        self.assertFalse(has_both_types(chart_ids([standard]), standard))
        self.assertEqual(
            title_for(standard, preparation(), "x", True), "【maimai】Song[STD] Master13.9 AP"
        )
        self.assertEqual(
            title_for(dx, preparation(), "x", True), "【maimai】Song[DX] Master13.9 AP"
        )
        self.assertEqual(title_for(standard, preparation(), "x"), "【maimai】Song Master13.9 AP")
        plus = preparation(combo="app", constant="13+?")
        utage = {"title": "Song", "difficulty_index": 0, "type": "utage"}
        self.assertEqual(title_for(utage, plus, ""), "【maimai】Song Utage13+? AP+")
        mirror = preparation(bvid=MIRROR_BV, combo="app")
        self.assertEqual(
            title_for(standard, mirror, "PA 9.31retsaM gnoS【iamiam】"),
            "+PA 9.31retsaM gnoS【iamiam】",
        )

    def test_combo_is_read_from_trailing_or_mirrored_marker(self):
        for title, bvid, expected in (
            ("【maimai】Song Expert13.0 AP", BV, "ap"),
            ("【maimai】Song Expert13.0 ＡＰ＋", BV, "app"),
            ("【maimai】Song Expert13.0 FC", BV, None),
            ("+PA 0.31retsaM gnoS【iamiam】", MIRROR_BV, "app"),
            ("PA 0.31retsaM gnoS【iamiam】", BV, None),
        ):
            self.assertEqual(title_combo(title, bvid), expected, title)


class ApplyTests(ServiceCase):
    def test_apply_writes_each_step_once_and_keeps_other_metadata(self):
        plan = self.prepare()
        self.assertEqual((self.client.calls, plan["title"]), ([], TITLE))
        self.assertEqual(self.apply(plan)["state"], "complete")
        self.assertEqual(self.client.calls, ["cover", "cover", "edit", "add"])
        posted = self.client.edit_payloads[0]
        self.assertEqual(
            (posted["desc"], posted["videos"]), (archive()["archive"]["desc"], archive()["videos"])
        )
        self.assertEqual(self.apply(plan)["state"], "complete")
        self.assertEqual(len(self.client.calls), 4)

    def test_archive_changed_after_preview_refuses_all_writes(self):
        plan = self.prepare()
        self.client.data["archive"]["desc"] = "Modified outside"
        self.assertEqual(self.apply(plan)["state"], "failed")
        self.assertEqual(self.client.calls, [])

    def test_uncertain_edit_resumes_only_when_read_back_shows_it(self):
        plan = self.prepare()
        self.client.fail, self.client.delay_edit = "edit", True
        job = self.apply(plan)
        self.assertEqual(job["state"], "uncertain")
        self.client.fail = None
        self.assertEqual(self.retry(job)["state"], "uncertain")  # Not visible: never resent.
        posted = self.client.edit_payloads[0]  # The edit becomes visible late.
        self.client.data["archive"].update({k: posted[k] for k in ("cover", "cover43", "title")})
        self.assertEqual(self.retry(job)["state"], "complete")
        self.assertEqual(self.client.calls.count("edit"), 1)

    def test_collection_timeout_never_blindly_repeats_add(self):
        self.client.fail = "add"
        job = self.apply(self.prepare())
        self.client.fail = None
        self.assertEqual(self.retry(job)["state"], "uncertain")
        self.assertEqual(self.client.calls.count("add"), 1)
        self.client.collection_members[10] = [{"id": 333, "aid": 456}]
        self.assertEqual(self.retry(job)["state"], "complete")

    def test_order_is_complete_only_after_full_read_back(self):
        rows = [episode(200, "Late"), episode(100, "Early")]
        self.store.extra_charts = [
            chart("200:3", "Late", "PRiSM"),
            chart("100:3", "Early", "GreeN"),
        ]
        self.client.extra_videos = [{"bvid": row["bvid"], "title": row["title"]} for row in rows]
        self.client.collection_members[10] = rows
        plan = self.prepare()
        self.assertEqual([row["aid"] for row in plan["order"]["desired"]], [100, 456, 200])
        self.client.fail = "sort_unapplied"
        job = self.apply(plan)
        self.client.fail = None
        self.assertEqual(self.retry(job)["state"], "uncertain")
        self.assertEqual(self.client.calls.count("sort"), 1)
        members = {row["aid"]: row for row in self.client.collection_members[10]}
        self.client.collection_members[10] = [members[aid] for aid in (100, 456, 200)]
        job = self.retry(job)
        self.assertEqual((job["state"], job["steps"]["order"]), ("complete", "verified"))


class StaleTitleTests(ServiceCase):
    def stale(self, title):
        self.client.data["archive"]["title"] = title
        self.client.videos = lambda page: {
            "items": [{"bvid": BV, "aid": 456, "title": title, "created_at": 1}],
            "has_more": False,
        }
        return self.service.stale()["items"]

    def test_bound_video_is_stale_after_constant_change_or_new_dx_sibling(self):
        self.assertEqual(self.stale(TITLE), [])  # Unbound videos are never reported.
        self.service.bind_chart(1234, BV, self.store.chart_value, TITLE)
        self.assertEqual(self.stale(TITLE), [])
        self.store.chart_value["constant"] = 14.0
        [item] = self.stale(TITLE)
        self.assertEqual(item["proposed_title"], "【maimai】Example Master14.0 AP")
        self.assertEqual([r["code"] for r in item["reasons"]], ["constant"])
        self.store.chart_value["constant"] = 13.9
        self.store.extra_charts = [self.store.chart_value | {"chart_key": "10123:3", "type": "dx"}]
        [item] = self.stale(TITLE)
        self.assertEqual(item["proposed_title"], "【maimai】Example[STD] Master13.9 AP")
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
