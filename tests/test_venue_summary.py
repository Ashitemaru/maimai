import unittest

from backend.venue_summary import summarize_venues


class VenueSummaryTest(unittest.TestCase):
    def test_current_level_confirmed_ap_and_pending_are_separate(self):
        charts = [
            {"venue": "【上海】甲", "is_ap": True, "level": "13", "constant": 13.9},
            {"venue": "【上海】甲", "is_ap": True, "level": "14+"},
            {"venue": "【上海】甲", "is_ap": True, "level": "15"},
            {"venue": "【北京】乙", "is_ap": False, "is_complete": True, "level": "13+"},
            {"venue": "", "is_ap": True, "level": "14"},
            {"venue": "", "is_ap": False, "pending_record": {"venue": "【北京】乙"}},
            {
                "venue": "【上海】甲",
                "is_ap": True,
                "level": "12+",
                "pending_record": {"venue": "【北京】乙"},
            },
        ]
        result = summarize_venues(
            charts,
            ["【北京】乙", "空机厅"],
            [{"venue": "【北京】乙"}, {"venue": "【北京】乙"}],
        )
        rows = {item["name"]: item for item in result["items"]}
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["totals"]["13"], 1)
        self.assertEqual(result["totals"]["14+/15"], 2)
        self.assertEqual(rows["【北京】乙"]["total"], 0)
        self.assertEqual(rows["【北京】乙"]["pending_count"], 2)
        self.assertEqual(result["pending_count"], 2)
        self.assertEqual(rows["空机厅"]["city"], "未分类")

    def test_pending_records_do_not_require_a_current_catalog_chart(self):
        result = summarize_venues(
            [], pending_records=[{"venue": "【测试】待核准机厅"}, {"venue": ""}]
        )
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["total"], 0)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["name"], "【测试】待核准机厅")
        self.assertEqual(result["items"][0]["pending_count"], 1)
        self.assertEqual(result["items"][0]["total"], 0)

    def test_unlisted_level_is_visible_and_venue_names_remain_exact(self):
        result = summarize_venues(
            [
                {"venue": "【广州】雁窝", "is_ap": True, "level": "12"},
                {"venue": "【广州】天河宴窝", "is_ap": True, "level": "13+"},
            ]
        )
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["levels"][-1], {"value": "other", "label": "其他"})
        self.assertEqual(result["totals"]["other"], 1)
        self.assertEqual(sum(result["totals"].values()), result["total"])

    def test_empty_summary_has_requested_columns(self):
        result = summarize_venues([])
        self.assertEqual(
            [item["value"] for item in result["levels"]], ["12+", "13", "13+", "14", "14+/15"]
        )
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
