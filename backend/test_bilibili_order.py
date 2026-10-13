from __future__ import annotations

import json
import unittest

import httpx

from backend import test_bilibili as fixtures
from backend.bilibili_client import BilibiliClient, BilibiliError
from backend.bilibili_order import (
    ChartResolver,
    chart_identity,
    order_report,
    parsed_title,
    section_snapshot,
)

BV = fixtures.BV
preparation = fixtures.preparation


def chart(key, song, version="UNiVERSE", kind="standard", difficulty=3):
    return {
        "chart_key": key,
        "title": song,
        "type": kind,
        "difficulty_index": difficulty,
        "jp_release_version": version,
    }


def episode(aid, song, *, difficulty="Master", marker=""):
    return {
        "episode_id": aid + 1000,
        "aid": aid,
        "cid": aid + 2000,
        "bvid": f"BV{aid:010d}",
        "title": f"【maimai】{song}{marker} {difficulty}13.9 AP",
    }


def snapshot(rows):
    return {"section": {"id": 10, "seasonId": 1, "title": "13.9", "type": 0}, "episodes": rows}


def resolver(charts, episodes, bindings=None):
    latest = {row["bvid"]: row["title"] for row in episodes}
    return ChartResolver(charts, bindings or {}, latest.get)


class OrderResolverTests(unittest.TestCase):
    def test_plus_is_distinct_and_remaster_uses_own_version(self):
        rows = [episode(1, "A", difficulty="Re:Master"), episode(2, "Z"), episode(3, "B")]
        charts = [
            chart("1:4", "A", "PRiSM", difficulty=4),
            chart("2:3", "Z", "PiNK"),
            chart("3:3", "B", "PiNK PLUS"),
        ]
        report = order_report(snapshot(rows), resolver(charts, rows), season_title="Test")
        self.assertEqual([item["aid"] for item in report["desired"]], [2, 3, 1])
        self.assertEqual(report["status"], "changes")

    def test_nfkc_casefold_and_equal_names_are_stable(self):
        rows = [episode(1, "Ｂeta"), episode(2, "ALPHA"), episode(3, "alpha")]
        charts = [chart("1:3", "Beta"), chart("2:3", "alpha")]
        report = order_report(snapshot(rows), resolver(charts, rows), season_title="Test")
        self.assertEqual([item["aid"] for item in report["desired"]], [2, 3, 1])

    def test_latest_title_disambiguates_std_dx_without_constant_guess(self):
        row = episode(1, "Dragoon")
        charts = [
            chart("1:3", "Dragoon", "ORANGE"),
            chart("10001:3", "Dragoon", "PRiSM", kind="dx"),
        ]
        resolved = ChartResolver(charts, {}, lambda _: "【maimai】Dragoon[STD] Master13.4 AP")
        selected, problem = resolved.resolve(row)
        self.assertIsNone(problem)
        self.assertEqual(selected["chart_key"], "1:3")
        ambiguous = ChartResolver(charts, {}, lambda _: row["title"])
        self.assertIn("STD/DX", ambiguous.resolve(row)[1])

    def test_unique_but_stale_episode_title_always_checks_latest(self):
        row = episode(1, "Old")
        resolved = ChartResolver(
            [chart("1:3", "Old"), chart("2:3", "New")], {}, lambda _: "【maimai】New Master13.9 AP"
        )
        self.assertEqual(resolved.resolve(row)[0]["chart_key"], "2:3")

    def test_approved_binding_supports_exact_historical_spelling_and_blocks_rename(self):
        value = chart("1:3", "Correct title")
        row = episode(1, "Historic spelling")
        binding = {
            "chart_key": value["chart_key"],
            "chart_identity": chart_identity(value),
            "title_identity": parsed_title(row["title"], row["bvid"]),
            "observed_title": row["title"],
            "source": "approved_cover_manifest",
        }
        resolved = resolver([value], [row], {row["bvid"]: binding})
        self.assertEqual(resolved.resolve(row)[0], value)
        resolved.latest_titles = lambda _: "【maimai】Other Master13.9 AP"
        self.assertIsNotNone(resolved.resolve(row)[1])

    def test_unparsed_bound_titles_are_not_all_equivalent(self):
        value = chart("1:3", "Correct")
        row = episode(1, "Other") | {"title": "custom"}
        binding = {
            "chart_key": "1:3",
            "chart_identity": chart_identity(value),
            "title_identity": None,
            "observed_title": "custom",
            "source": "user",
        }
        resolved = resolver([value], [row], {row["bvid"]: binding})
        self.assertEqual(resolved.resolve(row)[0], value)
        resolved.latest_titles = lambda _: "different custom"
        self.assertIsNotNone(resolved.resolve(row)[1])

    def test_mirrored_title_uses_forward_song_identity(self):
        title = "PA 9.31retsaM ｒｔｙ .taef スーバリクャジノマア【iamiam】"
        parsed = parsed_title(title, "BV1bJouBSEiH")
        self.assertEqual(parsed["song"], "アマノジャクリバース feat. ytr")
        self.assertEqual(parsed["difficulty"], 3)

    def test_unknown_versions_block_multiple_but_singleton_needs_no_comparison(self):
        row = episode(1, "CYCLES", difficulty="Utage")
        row["title"] = "【maimai】CYCLES Utage13? AP"
        unknown = chart("100230:0", "[宴]CYCLES", None, "utage", 0)
        single = order_report(snapshot([row]), resolver([unknown], [row]), season_title="宴")
        self.assertEqual(single["status"], "ordered")
        other = episode(2, "Normal")
        multiple = order_report(
            snapshot([row, other]),
            resolver([unknown, chart("2:3", "Normal")], [row, other]),
            season_title="Test",
        )
        self.assertEqual(multiple["status"], "blocked")
        self.assertEqual(len(multiple["unmatched"]), 1)

    def test_section_schema_rejects_duplicates_and_preserves_zero_type(self):
        row = episode(1, "Song")
        raw = {
            "section": snapshot([])["section"],
            "episodes": [
                {"id": row["episode_id"], **{k: v for k, v in row.items() if k != "episode_id"}}
            ],
        }
        self.assertEqual(section_snapshot(raw, section_id=10, season_id=1)["section"]["type"], 0)
        raw["episodes"] *= 2
        with self.assertRaises(BilibiliError):
            section_snapshot(raw, section_id=10, season_id=1)


class OrderServiceTests(unittest.TestCase):
    setUp = fixtures.BilibiliServiceTests.setUp
    prepare = fixtures.BilibiliServiceTests.prepare
    apply = fixtures.BilibiliServiceTests.apply

    def populate(self):
        rows = [episode(200, "Late"), episode(100, "Early")]
        self.store.extra_charts = [
            chart("200:3", "Late", "PRiSM"),
            chart("100:3", "Early", "GreeN"),
        ]
        self.client.extra_videos = [{"bvid": row["bvid"], "title": row["title"]} for row in rows]
        self.client.collection_members[10] = [
            {"id": row["episode_id"], **{k: v for k, v in row.items() if k != "episode_id"}}
            for row in rows
        ]
        return rows

    def test_complete_only_after_full_order_readback_preserves_section_type(self):
        self.populate()
        plan = self.prepare()
        self.assertEqual([row["aid"] for row in plan["order"]["desired"]], [100, 456, 200])
        self.assertEqual(plan["order"]["target_position"], 2)
        result = self.apply(plan)
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(result["steps"]["order"], "verified")
        self.assertEqual(self.client.calls, ["cover", "cover", "edit", "add", "sort"])
        self.assertEqual(
            [row["aid"] for row in self.client.collection_members[10]], [100, 456, 200]
        )

    def test_read_only_check_does_not_write_and_prepare_saves_only_target_binding(self):
        self.populate()
        report = self.service.collection_order()
        self.assertEqual(report["sections"][0]["status"], "changes")
        self.assertEqual(self.client.calls, [])
        with self.store.db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM bilibili_chart_bindings").fetchone()[0], 0
            )
        self.prepare()
        with self.store.db() as db:
            rows = db.execute("SELECT bvid FROM bilibili_chart_bindings").fetchall()
        self.assertEqual([row[0] for row in rows], [BV])

    def test_section_stale_or_catalog_stale_aborts_before_upload(self):
        self.populate()
        plan = self.prepare()
        self.client.collection_members[10].reverse()
        with self.assertRaises(BilibiliError):
            self.apply(plan)
        self.assertEqual(self.client.calls, [])
        self.client.collection_members[10].reverse()
        self.store.extra_charts[0]["jp_release_version"] = "maimai"
        with self.assertRaises(BilibiliError):
            self.apply(plan)
        self.assertEqual(self.client.calls, [])

    def test_real_title_change_with_unchanged_section_aborts_before_upload(self):
        self.populate()
        plan = self.prepare()
        self.client.extra_videos[0]["title"] = "【maimai】Other song Master13.9 AP"
        with self.assertRaises(BilibiliError):
            self.apply(plan)
        self.assertEqual(self.client.calls, [])

    def test_unknown_order_timeout_readback_recovers_without_mutation_repeat(self):
        self.populate()
        plan = self.prepare()
        self.client.fail = "sort"
        result = self.apply(plan)
        self.assertEqual(result["state"], "uncertain")
        self.client.fail = None
        result = self.service.retry(result["job_id"])
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(self.client.calls.count("sort"), 1)
        self.assertEqual(self.client.calls.count("edit"), 1)
        self.assertEqual(self.client.calls.count("add"), 1)

    def test_sent_order_can_be_verified_after_catalog_changes_without_reposting(self):
        self.populate()
        self.client.fail = "sort"
        result = self.apply(self.prepare())
        self.client.fail = None
        self.store.extra_charts[0]["jp_release_version"] = "maimai"
        result = self.service.retry(result["job_id"])
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(self.client.calls.count("sort"), 1)
        self.assertEqual(self.client.calls.count("add"), 1)

    def test_verified_collection_removed_externally_is_not_readded_on_retry(self):
        self.populate()
        self.client.fail = "sort_unapplied"
        result = self.apply(self.prepare())
        self.client.fail = None
        self.client.collection_members[10] = [
            row for row in self.client.collection_members[10] if row["aid"] != 456
        ]
        self.client.data["in_season"] = False
        result = self.service.retry(result["job_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.client.calls.count("add"), 1)
        self.assertEqual(self.client.calls.count("sort"), 1)

    def test_unknown_order_not_visible_never_resends(self):
        self.populate()
        self.client.fail = "sort_unapplied"
        result = self.apply(self.prepare())
        self.client.fail = None
        self.assertEqual(self.service.retry(result["job_id"])["state"], "uncertain")
        self.assertEqual(self.client.calls.count("sort"), 1)

    def test_unexpected_added_member_stops_order_without_removal(self):
        self.populate()
        original = self.client.add_episode

        def adding(section_id, data):
            original(section_id, data)
            unexpected = episode(300, "Unrelated")
            self.client.collection_members[section_id].append(
                {
                    "id": unexpected["episode_id"],
                    **{k: v for k, v in unexpected.items() if k != "episode_id"},
                }
            )

        self.client.add_episode = adding
        result = self.apply(self.prepare())
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("sort", self.client.calls)
        self.assertNotIn("remove", self.client.calls)

    def test_old_plan_without_order_is_rejected(self):
        plan = self.prepare()
        with self.store.db() as db:
            row = db.execute("SELECT payload FROM bilibili_plans").fetchone()
            value = json.loads(row[0])
            value.pop("_order")
            db.execute("UPDATE bilibili_plans SET payload=?", (json.dumps(value),))
        with self.assertRaises(BilibiliError):
            self.apply(plan)
        self.assertEqual(self.client.calls, [])

    def test_unknown_same_section_job_blocks_other_video(self):
        self.populate()
        self.client.fail = "sort_unapplied"
        self.apply(self.prepare())
        self.client.fail = None
        second_bv = "BV9999999999"
        self.client.data["archive"].update(aid=999, bvid=second_bv, title="Second video")
        body = preparation().model_copy(update={"bvid": second_bv})
        plan = self.service.prepare(body)
        before = len(self.client.calls)
        with self.assertRaises(BilibiliError):
            self.apply(plan)
        self.assertEqual(len(self.client.calls), before)

    def test_sort_client_preserves_zero_type_and_sends_all_episode_ids(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0})

        client = BilibiliClient(
            [{"name": "bili_jct", "value": "fake-csrf", "domain": ".bilibili.com"}],
            transport=httpx.MockTransport(handle),
        )
        try:
            header = {"id": 10, "seasonId": 1, "title": "13.9", "type": 0}
            client.sort_section(header, [77, 22, 99])
            body = json.loads(requests[0].content)
            self.assertEqual(body["section"], header)
            self.assertEqual(
                body["sorts"], [{"id": 77, "sort": 1}, {"id": 22, "sort": 2}, {"id": 99, "sort": 3}]
            )
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
