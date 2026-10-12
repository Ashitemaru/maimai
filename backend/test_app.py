import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.catalog_decisions import StaleDecisionError
from backend.catalog_updates import BusyError
from backend.store import ChartIndex, Store, normalized, summarize
from tests.fixtures.catalog import (
    CHART_COUNT,
    HIGH_COMPLETE,
    IMPORTED_COUNT,
    LEGACY_COMPLETE,
    LOW_COUNT,
)
from tests.support import ROOT, create_app


class FakeUpdater:
    def __init__(self):
        self.calls = []
        self.error = None
        self.closed = False

    def status(self):
        return {"state": "idle", "calls": self.calls}

    def operation(self, name):
        if self.error:
            raise self.error
        self.calls.append(name)
        return {"state": "running", "operation": name}

    def start(self):
        return self.operation("update")

    def retry_remy(self, recheck=False):
        return self.operation("recheck" if recheck else "remy")

    def decide(self, value):
        raise ValueError("旧裁决接口已停用，请一次提交全部冲突字段")

    def record_review(self):
        return {"records": [], "revision": "r1", "decision_revision": 0}

    def decide_record(self, value):
        self.last_record = value
        return self.operation("record-decision")

    def decide_records(self, value):
        self.last_records = value
        return self.operation("record-batch")

    def manual_records(self):
        return {"records": [], "revision": "r1", "decision_revision": 0}

    def revoke_record(self, value):
        self.last_revoke = value
        return self.operation("record-revoke")

    def reapply_decisions(self):
        return self.operation("reapply")

    def rollback(self):
        return self.operation("rollback")

    def close(self):
        self.closed = True


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        catalog = Path(self.tmp.name) / "fixture-catalog"
        catalog.mkdir()
        shutil.copy2(ROOT / "data/catalog/catalog.sqlite", catalog / "catalog.sqlite")
        self.store = Store(runtime=self.tmp.name, catalog_dir=catalog)
        self.updater = FakeUpdater()
        self.client = TestClient(create_app(self.store, updater=self.updater))

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def put_record(self, key, *, json=None, **kwargs):
        chart = self.store.chart(key)
        payload = {
            "expected_revision": chart["record_revision"] if chart else 0,
            "request_id": str(uuid4()),
            **(json or {}),
        }
        return self.client.put(f"/api/charts/{key}/ap-record", json=payload, **kwargs)

    def fixture_chart(self, song_id, **changes):
        chart = {
            **self.store.chart("22:standard:4"),
            "song_id": song_id,
            "divingfish_id": song_id,
            "title": f"Test {song_id}",
            "type": "standard",
            "difficulty_index": 3,
            "constant": 13.3,
            "level": "13",
            "group": "high",
            "in_original_table": True,
            "is_complete": False,
            "is_ap": False,
            "conflict": False,
            "legacy_complete": False,
            "status": "incomplete",
            "achievement": None,
            "provider_achievement": None,
            "manual_ap": None,
            "venue": "",
            "jp_release_version": "PiNK",
            "jp_release_version_candidate": None,
            "release_status": "resolved",
            "jp_release_provider": "arcade_songs",
            **changes,
        }
        chart["chart_key"] = f"{song_id}:{chart['type']}:{chart['difficulty_index']}"
        chart["_search"] = normalized(chart["title"])
        return chart

    def test_constant_sort_uses_numeric_song_id_before_title(self):
        fixtures = [
            self.fixture_chart(10, title="A"),
            self.fixture_chart(2, title="Z"),
            self.fixture_chart(20, constant=14.0),
            self.fixture_chart(1, constant=12.0),
            self.fixture_chart(2, title="B", type="dx"),
        ]
        with patch.object(self.store, "all_charts", return_value=fixtures):
            for sort, ids in (
                ("constant_desc", [20, 2, 2, 10, 1]),
                ("constant_asc", [1, 2, 2, 10, 20]),
            ):
                with self.subTest(sort=sort):
                    items = self.client.get("/api/charts", params={"sort": sort}).json()["items"]
                    self.assertEqual([c["song_id"] for c in items], ids)
                    self.assertEqual(
                        [c["type"] for c in items if c["song_id"] == 2], ["dx", "standard"]
                    )

    def test_achievement_sort_uses_manual_then_provider_keeps_zero_and_nulls_last(self):
        fixtures = [
            self.fixture_chart(10, achievement=99, provider_achievement=101),
            self.fixture_chart(20, achievement=0, provider_achievement=100),
            self.fixture_chart(2, provider_achievement=100.2),
            self.fixture_chart(4, provider_achievement=101),
            self.fixture_chart(3, constant=15),
            self.fixture_chart(1, achievement=100.2, provider_achievement=98),
            self.fixture_chart(30, provider_achievement=0),
            self.fixture_chart(6, constant=12),
        ]
        with patch.object(self.store, "all_charts", return_value=fixtures):
            for sort, ids in (
                ("achievement_desc", [4, 1, 2, 10, 20, 30, 3, 6]),
                ("achievement_asc", [20, 30, 10, 1, 2, 4, 3, 6]),
            ):
                with self.subTest(sort=sort):
                    response = self.client.get("/api/charts", params={"sort": sort})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual([c["song_id"] for c in response.json()["items"]], ids)
                    paged = []
                    for offset in range(0, len(fixtures), 2):
                        page = self.client.get(
                            "/api/charts", params={"sort": sort, "offset": offset, "limit": 2}
                        ).json()
                        self.assertEqual(page["total"], len(fixtures))
                        paged.extend(c["song_id"] for c in page["items"])
                    self.assertEqual(paged, ids)

    def test_ap_sort_uses_confirmed_ap_not_legacy_completion_and_stable_ties(self):
        fixtures = [
            self.fixture_chart(10, is_ap=True, is_complete=True, status="app"),
            self.fixture_chart(2, is_ap=True, is_complete=True, status="ap"),
            self.fixture_chart(
                2, is_ap=True, is_complete=True, status="ap", type="dx", difficulty_index=4
            ),
            self.fixture_chart(
                2, is_ap=True, is_complete=True, status="ap", type="dx", difficulty_index=3
            ),
            self.fixture_chart(1, constant=14, manual_ap=False, provider_fc="app"),
            self.fixture_chart(
                3, constant=15, is_complete=True, legacy_complete=True, status="legacy"
            ),
            self.fixture_chart(4, constant=12),
        ]
        ap = ["2:dx:3", "2:dx:4", "2:standard:3", "10:standard:3"]
        non_ap = ["3:standard:3", "1:standard:3", "4:standard:3"]
        with patch.object(self.store, "all_charts", return_value=fixtures):
            for sort, keys in (("ap_desc", ap + non_ap), ("ap_asc", non_ap + ap)):
                with self.subTest(sort=sort):
                    paged = []
                    for offset in range(0, len(fixtures), 2):
                        response = self.client.get(
                            "/api/charts", params={"sort": sort, "offset": offset, "limit": 2}
                        )
                        self.assertEqual(response.status_code, 200)
                        page = response.json()
                        self.assertEqual(page["total"], len(fixtures))
                        paged.extend(c["chart_key"] for c in page["items"])
                    self.assertEqual(paged, keys)

    def test_achievement_sort_ties_use_constant_id_format_and_difficulty(self):
        fixtures = [
            self.fixture_chart(2, achievement=100),
            self.fixture_chart(2, achievement=100, type="dx", difficulty_index=4),
            self.fixture_chart(2, achievement=100, type="dx", difficulty_index=3),
            self.fixture_chart(10, achievement=100, constant=14),
            self.fixture_chart(1, achievement=100),
        ]
        with patch.object(self.store, "all_charts", return_value=fixtures):
            for sort in ("achievement_desc", "achievement_asc"):
                with self.subTest(sort=sort):
                    items = self.client.get("/api/charts", params={"sort": sort}).json()["items"]
                    self.assertEqual(
                        [c["chart_key"] for c in items],
                        ["10:standard:3", "1:standard:3", "2:dx:3", "2:dx:4", "2:standard:3"],
                    )

    def test_summary_and_table_share_combined_filters_and_empty_results(self):
        base = {
            "title": "Shared title",
            "type": "dx",
            "venue": "Test arcade",
            "is_complete": True,
            "is_ap": True,
            "status": "ap",
        }
        fixtures = [self.fixture_chart(1, **base)]
        exclusions = [
            {"title": "Other title"},
            {"group": "low"},
            {"difficulty_index": 2},
            {"type": "standard"},
            {"is_complete": False, "is_ap": False, "status": "incomplete"},
            {"constant": 13.2},
            {"constant": 13.4},
            {"level": "13+"},
            {"venue": "Other arcade"},
            {"jp_release_version": "PiNK PLUS"},
        ]
        fixtures += [
            self.fixture_chart(i + 2, **(base | changes)) for i, changes in enumerate(exclusions)
        ]
        params = {
            "q": "shared",
            "group": "high",
            "difficulty": 3,
            "type": "dx",
            "status": "complete",
            "constant_min": 13.3,
            "constant_max": 13.3,
            "level": "13",
            "venue": "Test",
            "version": "PiNK",
        }
        with patch.object(self.store, "all_charts", return_value=fixtures):
            listing = self.client.get("/api/charts", params=params).json()
            summary = self.client.get("/api/summary", params=params).json()
            self.assertEqual([c["song_id"] for c in listing["items"]], [1])
            self.assertEqual(listing["total"], summary["total"])
            for key, value in summarize(listing["items"]).items():
                self.assertEqual(summary[key], value)
            self.assertEqual(summary["groups"]["high"]["total"], 10)
            self.assertEqual(summary["groups"]["low"]["total"], 1)
            self.assertEqual([(v["level"], v["total"]) for v in summary["levels"]], [("13", 1)])
            self.assertEqual(
                [(v["constant"], v["total"]) for v in summary["constants"]], [(13.3, 1)]
            )
            options = self.client.get("/api/chart-filters", params={"group": "high"}).json()
            self.assertEqual(options["constants"], [13.2, 13.3, 13.4])
            self.assertEqual(options["levels"], ["13", "13+"])
            empty_params = params | {"version": "GreeN"}
            empty = self.client.get("/api/summary", params=empty_params).json()
            self.assertEqual(self.client.get("/api/charts", params=empty_params).json()["total"], 0)
            for key in ("total", "complete", "ap", "remaining", "conflicts", "legacy_complete"):
                self.assertEqual(empty[key], 0)
            self.assertEqual((empty["levels"], empty["constants"]), ([], []))

    def test_completion_table_has_all_charts_and_matches_every_list_sort(self):
        fixtures = [
            self.fixture_chart(
                90000 + i // 2,
                id=90000 + i // 2,
                difficulty=i % 2 + 2,
                difficulty_index=i % 2 + 2,
                title=f"Grid {23 - i:02d}",
                display_title=f"Grid {23 - i:02d}",
                constant=12.7 + (i % 3) / 10,
                level="12+",
                is_complete=i % 3 != 0,
                is_ap=i % 3 == 1,
                legacy_complete=i % 3 == 2,
                status=("incomplete", "app", "legacy")[i % 3],
                achievement=101 if i % 3 == 1 else None,
                provider_achievement=99.0 + i / 100 if i % 3 == 0 else None,
                venue="Private arcade",
                note="Private note",
                manual_date="2026-10-01",
            )
            for i in range(24)
        ]
        allowed = {
            "id",
            "difficulty",
            "chart_key",
            "title",
            "display_title",
            "type",
            "difficulty_name",
            "level",
            "constant",
            "jacket_url",
            "is_complete",
            "is_ap",
            "status",
            "conflict",
            "achievement",
            "provider_achievement",
            "pending_record",
            "record_revision",
            "venue_pending",
        }
        with patch.object(self.store, "all_charts", return_value=fixtures):
            for sort in (
                "constant_desc",
                "constant_asc",
                "achievement_desc",
                "achievement_asc",
                "ap_desc",
                "ap_asc",
                "title",
            ):
                with self.subTest(sort=sort):
                    response = self.client.get("/api/completion-table", params={"sort": sort})
                    self.assertEqual(response.status_code, 200)
                    result = response.json()
                    listing = self.client.get(
                        "/api/charts", params={"sort": sort, "limit": 6000}
                    ).json()
                    self.assertEqual(result["total"], 24)
                    self.assertEqual(
                        [c["chart_key"] for c in result["items"]],
                        [c["chart_key"] for c in listing["items"]],
                    )
                    self.assertEqual(len({(c["id"], c["difficulty"]) for c in result["items"]}), 24)
                    for item in result["items"]:
                        self.assertEqual(set(item), allowed)
                    for field, value in summarize(fixtures).items():
                        self.assertEqual(result["summary"][field], value)
                    self.assertNotIn("Private", response.text)
                    self.assertIsNotNone(datetime.fromisoformat(result["generated_at"]).tzinfo)
        self.assertEqual(
            self.client.get("/api/completion-table", params={"sort": "invalid"}).status_code, 422
        )

    def test_completion_table_shares_all_filters_and_summary_snapshot(self):
        base = {
            "title": "Match me",
            "type": "dx",
            "venue": "One arcade",
            "is_complete": True,
            "is_ap": False,
            "legacy_complete": True,
            "status": "legacy",
            "_cn_version_group": 25500,
        }
        match = self.fixture_chart(80001, **base)
        exclusions = [
            {"title": "Other title"},
            {"group": "low"},
            {"difficulty_index": 2},
            {"type": "standard"},
            {"is_complete": False, "status": "incomplete"},
            {"constant": 13.2},
            {"constant": 13.4},
            {"level": "13+"},
            {"venue": "Other arcade"},
            {"jp_release_version": "PiNK PLUS"},
            {"_cn_version_group": 25000},
        ]
        fixtures = [match] + [
            self.fixture_chart(80002 + i, **(base | changes))
            for i, changes in enumerate(exclusions)
        ]
        params = {
            "q": "match",
            "group": "high",
            "difficulty": 3,
            "type": "dx",
            "status": "complete",
            "constant_min": 13.3,
            "constant_max": 13.3,
            "level": "13",
            "venue": "One",
            "version": "PiNK",
            "cn_version": 25500,
        }
        # A second read would see a different catalog; counts and items must use the first one.
        with patch.object(self.store, "all_charts", side_effect=[fixtures, []]) as read:
            result = self.client.get("/api/completion-table", params=params).json()
            read.assert_called_once_with()
        self.assertEqual([c["chart_key"] for c in result["items"]], [match["chart_key"]])
        self.assertEqual(result["summary"]["complete"], 1)
        self.assertEqual(result["summary"]["ap"], 0)
        with patch.object(self.store, "all_charts", return_value=fixtures):
            listing = self.client.get("/api/charts", params=params).json()
            summary = self.client.get("/api/summary", params=params).json()
            self.assertEqual(result["total"], listing["total"])
            for field, value in result["summary"].items():
                self.assertEqual(value, summary[field])
            for status in ("all", "complete", "incomplete", "ap", "legacy", "conflict"):
                with self.subTest(status=status):
                    options = {"status": status}
                    grid = self.client.get("/api/completion-table", params=options).json()
                    rows = self.client.get("/api/charts", params=options).json()
                    self.assertEqual(
                        [c["chart_key"] for c in grid["items"]],
                        [c["chart_key"] for c in rows["items"]],
                    )
            empty = self.client.get(
                "/api/completion-table", params=params | {"version": "GreeN"}
            ).json()
            self.assertEqual((empty["items"], empty["total"]), ([], 0))
            self.assertEqual((empty["summary"]["levels"], empty["summary"]["constants"]), ([], []))

    def test_completion_table_reads_full_catalog_without_personal_detail(self):
        result = self.client.get("/api/completion-table").json()
        self.assertEqual(len(result["items"]), len(self.store.catalog))
        self.assertEqual(result["total"], len(result["items"]))
        self.assertEqual(result["total"], CHART_COUNT)
        listing = self.client.get("/api/charts").json()
        self.assertEqual(len(listing["items"]), 100)
        self.assertGreater(result["total"], len(listing["items"]))
        self.assertEqual(result["summary"]["total"], result["total"])
        self.assertEqual(
            sum(group["total"] for group in result["summary"]["constants"]), result["total"]
        )
        self.assertEqual(
            sum(group["total"] for group in result["summary"]["levels"]), result["total"]
        )
        self.assertEqual(
            result["summary"]["complete"], sum(c["is_complete"] for c in result["items"])
        )
        for item in result["items"]:
            for field in (
                "venue",
                "manual_venue",
                "note",
                "manual_date",
                "manual_updated_at",
                "warnings",
                "_search",
            ):
                self.assertNotIn(field, item)

    def test_version_uses_japanese_chart_debut_and_retains_unknowns(self):
        fixtures = [
            self.fixture_chart(1),
            self.fixture_chart(
                2, difficulty_index=4, jp_release_version="PRiSM", release_status="verified"
            ),
            self.fixture_chart(3, difficulty_index=4, jp_release_version=None),
            self.fixture_chart(
                4,
                jp_release_version=None,
                release_status="cn_source_candidate",
                type_release_version_candidate="maimai PiNK",
            ),
            self.fixture_chart(5, jp_release_version="PiNK PLUS", release_status="manual"),
            self.fixture_chart(6, jp_release_version="DX", release_status="conflict"),
            self.fixture_chart(7, jp_release_version="maimai", group="low"),
            self.fixture_chart(
                8,
                jp_release_version=None,
                jp_release_version_candidate="PiNK",
                release_status="song_source",
            ),
        ]
        with patch.object(self.store, "all_charts", return_value=fixtures):
            for version, ids in (
                ("PiNK", [1]),
                ("PRiSM", [2]),
                ("__unknown__", [3, 4, 8]),
                ("PiNK PLUS", [5]),
            ):
                with self.subTest(version=version):
                    params = {"group": "high", "version": version}
                    listing = self.client.get("/api/charts", params=params).json()
                    self.assertEqual([c["song_id"] for c in listing["items"]], ids)
                    self.assertEqual(
                        self.client.get("/api/summary", params=params).json()["total"], len(ids)
                    )
            options = self.client.get("/api/chart-filters", params={"group": "high"}).json()
            self.assertEqual(
                options["versions"],
                [
                    {"value": "PiNK", "label": "PiNK"},
                    {"value": "PiNK PLUS", "label": "PiNK PLUS"},
                    {"value": "DX", "label": "でらっくす"},
                    {"value": "PRiSM", "label": "PRiSM"},
                    {"value": "__unknown__", "label": "版本待核实"},
                ],
            )

    def test_cn_version_ranges_preserve_minor_updates_and_chart_identity(self):
        versions = [
            {"version": 19900, "title": "FiNALE"},
            {"version": 20000, "title": "舞萌DX"},
            *[
                {"version": 20000 + (year - 2020) * 1000, "title": f"舞萌DX {year}"}
                for year in range(2021, 2026)
            ],
            {"version": 25500, "title": "舞萌DX 2026"},
        ]
        catalog = ChartIndex(versions_cn=versions)
        base = self.store.catalog["22:standard:4"]
        raw_versions = [
            19999,
            20000,
            20100,
            20999,
            21000,
            24500,
            24999,
            25000,
            25499,
            25500,
            25504,
            25999,
            26000,
            None,
        ]
        for offset, raw in enumerate(raw_versions):
            ident = 90001 + offset
            key = f"{ident}:standard:3"
            catalog[key] = dict(
                base,
                chart_key=key,
                song_id=ident,
                difficulty_index=3,
                title=f"CN fixture {ident}",
                api_version=raw,
                jp_release_version="PiNK",
                _search=f"cn fixture {ident}",
            )
        # The same song's Re:MASTER has its own CN release, independent of JP.
        catalog["90002:standard:4"] = dict(
            catalog["90002:standard:3"],
            chart_key="90002:standard:4",
            difficulty_index=4,
            api_version=25504,
            jp_release_version="PRiSM",
        )
        self.store.install_catalog((catalog, {}))
        options = self.client.get("/api/chart-filters").json()
        self.assertEqual(
            options["cn_versions"],
            [
                {"value": "19900", "label": "FiNALE"},
                {"value": "20000", "label": "DX"},
                *[
                    {"value": str(20000 + (year - 2020) * 1000), "label": f"DX{year}"}
                    for year in range(2021, 2026)
                ],
                {"value": "25500", "label": "DX2026"},
            ],
        )
        for version, keys in [
            (19900, ["90001:standard:3"]),
            (20000, ["90002:standard:3", "90003:standard:3", "90004:standard:3"]),
            (21000, ["90005:standard:3"]),
            (24000, ["90006:standard:3", "90007:standard:3"]),
            (25000, ["90008:standard:3", "90009:standard:3"]),
            (
                25500,
                ["90002:standard:4", "90010:standard:3", "90011:standard:3", "90012:standard:3"],
            ),
            (20100, []),
            (26000, []),
        ]:
            with self.subTest(cn_version=version):
                params = {"cn_version": version}
                listing = self.client.get("/api/charts", params=params).json()
                summary = self.client.get("/api/summary", params=params).json()
                self.assertEqual([c["chart_key"] for c in listing["items"]], keys)
                self.assertEqual(listing["total"], summary["total"])
                self.assertEqual(summary["total"], len(keys))
                self.assertEqual(sum(c["total"] for c in summary["constants"]), len(keys))
                self.assertEqual(sum(c["total"] for c in summary["levels"]), len(keys))
                paged = []
                for offset in range(0, len(keys), 2):
                    page = self.client.get(
                        "/api/charts", params=params | {"offset": offset, "limit": 2}
                    ).json()
                    self.assertEqual(page["total"], len(keys))
                    paged.extend(c["chart_key"] for c in page["items"])
                self.assertEqual(paged, keys)
        self.assertEqual(self.client.get("/api/charts").json()["total"], len(catalog))
        self.assertEqual(
            self.client.get("/api/charts", params={"version": "PRiSM"}).json()["total"], 1
        )
        self.assertEqual(
            self.client.get("/api/charts", params={"version": "PRiSM", "cn_version": 20000}).json()[
                "total"
            ],
            0,
        )
        chart = self.client.get("/api/charts/90002:standard:4").json()
        self.assertEqual(chart["cn_version"], 25504)
        self.assertEqual(chart["jp_release_version"], "PRiSM")
        self.assertNotIn("_cn_version_group", chart)
        for bad in ("DX2026", 9999):
            self.assertEqual(
                self.client.get("/api/charts", params={"cn_version": bad}).status_code, 422
            )

    def test_cn_version_metadata_reloads_with_chart_snapshot_and_never_reads_live_disk(self):
        old = self.store._catalog_snapshot
        snapshot = Path(self.tmp.name) / "new-catalog"
        snapshot.mkdir()
        shutil.copy2(self.store.catalog_root / "catalog.sqlite", snapshot / "catalog.sqlite")
        metadata = [
            {"title": "舞萌DX", "version": 20000},
            {"title": "舞萌DX 2026", "version": 25500},
        ]
        with closing(sqlite3.connect(snapshot / "catalog.sqlite")) as conn, conn:
            conn.execute(
                "UPDATE metadata SET value_json=? WHERE key='versions_cn'", (json.dumps(metadata),)
            )
            conn.execute("UPDATE charts SET api_version=25504 WHERE chart_key='22:standard:4'")
        loaded = self.store.load_catalog(snapshot)
        expected = [{"value": "20000", "label": "DX"}, {"value": "25500", "label": "DX2026"}]
        self.assertIs(self.store._catalog_snapshot, old)
        self.store.install_catalog(loaded)
        captured = self.store.all_charts()
        self.assertEqual(self.client.get("/api/chart-filters").json()["cn_versions"], expected)
        self.assertEqual(self.client.get("/api/charts/22:standard:4").json()["cn_version"], 25504)
        # Changing files without publishing cannot change request-time metadata.
        with closing(sqlite3.connect(snapshot / "catalog.sqlite")) as conn, conn:
            conn.execute("DELETE FROM metadata WHERE key='versions_cn'")
            conn.execute("UPDATE charts SET api_version=20000 WHERE chart_key='22:standard:4'")
        self.assertEqual(self.client.get("/api/chart-filters").json()["cn_versions"], expected)
        self.assertEqual(self.client.get("/api/charts/22:standard:4").json()["cn_version"], 25504)
        self.store.install_catalog(self.store.load_catalog(snapshot))
        self.assertEqual(self.client.get("/api/chart-filters").json()["cn_versions"], [])
        self.assertEqual(
            self.client.get("/api/charts", params={"cn_version": 20000}).json()["total"], 0
        )
        self.assertEqual(self.client.get("/api/charts/22:standard:4").json()["cn_version"], 20000)
        # A reader already holding rows also retains that snapshot's version menu.
        with patch.object(self.store, "all_charts", return_value=captured):
            self.assertEqual(self.client.get("/api/chart-filters").json()["cn_versions"], expected)
            self.assertEqual(
                self.client.get("/api/charts/22:standard:4").json()["cn_version"], 25504
            )

    def test_chart_schema_reads_chart_cn_version_from_same_sqlite_metadata(self):
        directory = Path(self.tmp.name) / "chart-schema"
        directory.mkdir()
        with closing(sqlite3.connect(directory / "catalog.sqlite")) as conn, conn:
            conn.executescript(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value_json TEXT);"
                "CREATE TABLE charts(id INTEGER,difficulty INTEGER,type TEXT,data_json TEXT);"
            )
            conn.execute(
                "INSERT INTO metadata VALUES (?,?)",
                (
                    "versions_cn",
                    json.dumps(
                        [
                            {"title": "舞萌DX 2025", "version": 25000},
                            {"title": "舞萌DX 2026", "version": 25500},
                        ]
                    ),
                ),
            )
            for difficulty, version in ((3, 25004), (4, 25504)):
                chart = dict(
                    self.store.catalog["22:standard:4"],
                    id=90001,
                    difficulty=difficulty,
                    cn_version=version,
                    api_version=20000,
                    admission_status="admitted",
                    release_status="resolved",
                    jacket_path="jackets/22.png",
                    aliases=[],
                )
                conn.execute(
                    "INSERT INTO charts VALUES (?,?,?,?)",
                    (90001, difficulty, "standard", json.dumps(chart)),
                )
        self.store.install_catalog(self.store.load_catalog(directory))
        for version, difficulty in ((25000, 3), (25500, 4)):
            listing = self.client.get("/api/charts", params={"cn_version": version}).json()
            self.assertEqual([c["chart_key"] for c in listing["items"]], [f"90001:{difficulty}"])
            self.assertEqual(listing["items"][0]["cn_version"], version + 4)

    def test_legacy_decision_endpoint_cannot_bypass_whole_chart_gate(self):
        payload = {
            "base_catalog_revision": "r1",
            "base_decision_revision": 0,
            "object_key": "1:dx:ordinary",
            "kind": "version",
            "action": "set",
            "version": "DX",
        }
        self.assertEqual(self.client.post("/api/catalog/decisions", json=payload).status_code, 422)
        self.assertEqual(
            self.client.post(
                "/api/catalog/decisions", json=payload | {"date": "invalid"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/catalog/decisions",
                json=payload,
                headers={"Origin": "https://external.example"},
            ).status_code,
            403,
        )
        self.assertEqual(self.client.post("/api/catalog/decisions/reapply").status_code, 202)
        self.assertEqual(self.client.post("/api/catalog/remy/recheck").status_code, 202)

    def test_chart_decision_api_validates_primary_key_complete_payload_and_origin(self):
        self.assertEqual(self.client.get("/api/catalog/records/review").json()["records"], [])
        self.assertEqual(self.client.get("/api/catalog/records/decisions").json()["records"], [])
        payload = {
            "base_catalog_revision": "r1",
            "base_decision_revision": 0,
            "id": 10001,
            "difficulty": 3,
            "choices": [
                {"field_key": "artist", "option_id": "artist-a"},
                {"field_key": "constant", "option_id": "constant-b"},
            ],
        }
        response = self.client.post("/api/catalog/records/decisions", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(self.updater.last_record["choices"]), 2)
        self.assertEqual(self.updater.last_record["id"], 10001)
        for wrong in [
            payload | {"id": 0},
            payload | {"difficulty": 5},
            payload | {"unexpected": True},
            payload | {"song_id": 1},
            payload | {"identity_choices": []},
            payload | {"choices": []},
            payload
            | {"choices": [{"field_key": "artist", "action": "select", "option_id": "artist-a"}]},
            payload | {"choices": [{"field_key": "artist", "value": "forged"}]},
        ]:
            self.assertEqual(
                self.client.post("/api/catalog/records/decisions", json=wrong).status_code, 422
            )
        self.assertEqual(self.updater.calls.count("record-decision"), 1)
        self.updater.error = StaleDecisionError("stale")
        self.assertEqual(
            self.client.post("/api/catalog/records/decisions", json=payload).status_code, 409
        )
        self.updater.error = None
        self.assertEqual(
            self.client.post(
                "/api/catalog/records/decisions",
                json=payload,
                headers={"Origin": "https://external.example"},
            ).status_code,
            403,
        )
        revoke = {key: value for key, value in payload.items() if key != "choices"}
        self.assertEqual(
            self.client.post("/api/catalog/records/decisions/revoke", json=revoke).status_code, 202
        )
        self.assertIsNone(self.updater.last_revoke["field_keys"])
        self.assertEqual(
            self.client.post(
                "/api/catalog/records/decisions/revoke", json=revoke | {"field_keys": ["artist"]}
            ).status_code,
            202,
        )
        self.assertEqual(
            self.client.post(
                "/api/catalog/records/decisions/revoke", json=revoke | {"field_keys": []}
            ).status_code,
            422,
        )

    def test_chart_decision_batch_api_validates_all_records_and_origin(self):
        payload = {
            "base_catalog_revision": "r1",
            "base_decision_revision": 0,
            "records": [
                {
                    "id": 383,
                    "difficulty": 1,
                    "choices": [{"field_key": "title", "option_id": "title-lxns"}],
                },
                {
                    "id": 383,
                    "difficulty": 2,
                    "choices": [{"field_key": "title", "option_id": "title-fish"}],
                },
            ],
        }
        response = self.client.post("/api/catalog/records/decisions/batch", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(self.updater.last_records["records"]), 2)
        self.assertEqual(self.updater.last_records["records"][1]["difficulty"], 2)
        first, second = payload["records"]
        for wrong in [
            payload | {"records": []},
            payload | {"extra": True},
            payload | {"records": [first, second | {"difficulty": 5}]},
            payload | {"records": [first, second | {"choices": []}]},
            payload | {"records": [first, second | {"extra": True}]},
            payload
            | {
                "records": [
                    first,
                    second | {"choices": [{"field_key": "title", "value": "forged"}]},
                ]
            },
        ]:
            self.assertEqual(
                self.client.post("/api/catalog/records/decisions/batch", json=wrong).status_code,
                422,
            )
        self.assertEqual(self.updater.calls.count("record-batch"), 1)
        for error, code in [
            (StaleDecisionError("stale"), 409),
            (BusyError("busy"), 409),
            (ValueError("incomplete"), 422),
        ]:
            self.updater.error = error
            self.assertEqual(
                self.client.post("/api/catalog/records/decisions/batch", json=payload).status_code,
                code,
            )
        self.updater.error = None
        self.assertEqual(
            self.client.post(
                "/api/catalog/records/decisions/batch",
                json=payload,
                headers={"Origin": "https://external.example"},
            ).status_code,
            403,
        )

    def test_import_reconciles_and_keeps_legacy_ap_distinct(self):
        data = self.client.get("/api/summary").json()
        self.assertEqual(
            (data["total"], data["complete"], data["ap"]), (CHART_COUNT, LEGACY_COMPLETE, 0)
        )
        self.assertEqual(data["groups"]["low"]["total"], LOW_COUNT)
        self.assertEqual(data["groups"]["high"]["complete"], HIGH_COMPLETE)
        self.assertEqual(self.store.chart("1422:dx:0")["display_title"], "（空白曲名）")
        with self.store.db() as conn:
            labels = dict(
                conn.execute(
                    "SELECT chart_key,label FROM legacy WHERE chart_key IN ('131:standard:3','383:standard:3')"
                )
            )
        self.assertEqual(labels["131:standard:3"], "[MAS, SD] Link")
        self.assertEqual(labels["383:standard:3"], "[MAS, SD] Link(CoF)")
        self.assertEqual(sum(x["total"] for x in data["levels"]), CHART_COUNT)
        self.assertEqual(sum(x["complete"] for x in data["constants"]), LEGACY_COMPLETE)
        self.assertEqual(data["import_info"]["total"], IMPORTED_COUNT)

    def test_catalog_operations_route_errors_and_lifespan(self):
        app = create_app(self.store, updater=self.updater)
        self.assertIs(app.state.catalog_updates, self.updater)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/catalog/update").json()["state"], "idle")
            for route, operation in (
                ("update", "update"),
                ("remy/retry", "remy"),
                ("rollback", "rollback"),
            ):
                with self.subTest(route=route):
                    result = client.post(f"/api/catalog/{route}")
                    self.assertEqual(result.status_code, 202)
                    self.assertEqual(result.json()["operation"], operation)
                    self.updater.error = BusyError("更新正在进行")
                    self.assertEqual(client.post(f"/api/catalog/{route}").status_code, 409)
                    self.updater.error = ValueError("没有可回滚的版本")
                    self.assertEqual(client.post(f"/api/catalog/{route}").status_code, 422)
                    self.updater.error = None
            self.assertEqual(self.updater.calls, ["update", "remy", "rollback"])
            self.assertEqual(
                client.post(
                    "/api/catalog/update", headers={"Origin": "https://external.example"}
                ).status_code,
                403,
            )
        self.assertTrue(self.updater.closed)

    def test_catalog_reload_preserves_records_and_publishes_new_charts(self):
        base = Path(self.tmp.name) / "catalog"
        base.mkdir()
        shutil.copy2(ROOT / "data/catalog/catalog.sqlite", base / "catalog.sqlite")
        self.store.catalog_root = base
        self.store.reload_catalog()
        self.store.save_manual(
            "30:standard:0", {"ap": True, "venue": "", "note": "keep", "expected_revision": 0}
        )
        self.store.import_provider(
            {"records": [{"song_id": 10030, "level_index": 0, "fc": "ap", "achievements": 100.5}]}
        )
        personal_before = self.store.db_path.read_bytes()
        original = self.store._catalog_snapshot
        snapshot = base / "snapshots" / "new"
        snapshot.mkdir(parents=True)
        shutil.copy2(base / "catalog.sqlite", snapshot / "catalog.sqlite")
        with sqlite3.connect(snapshot / "catalog.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            song = dict(conn.execute("SELECT * FROM songs WHERE id=30").fetchone())
            song.update(id=90001, title="New chart after update")
            conn.execute(
                f"INSERT INTO songs VALUES ({','.join('?' for _ in song)})", tuple(song.values())
            )
            chart = dict(
                conn.execute("SELECT * FROM charts WHERE chart_key='30:standard:0'").fetchone()
            )
            chart.update(
                chart_key="90001:standard:0",
                song_id=90001,
                constant=12.7,
                level="12+",
                divingfish_id="90001",
            )
            conn.execute(
                f"INSERT INTO charts VALUES ({','.join('?' for _ in chart)})", tuple(chart.values())
            )
            conn.execute(
                "INSERT INTO external_ids VALUES('divingfish','90001',90001,'standard','same_id_standard','New chart after update')"
            )
            conn.execute("UPDATE charts SET constant=14.8 WHERE chart_key='30:standard:0'")
        loaded = self.store.load_catalog(snapshot)
        self.assertIs(self.store._catalog_snapshot, original)
        self.assertNotIn("90001:standard:0", self.store.catalog)
        (base / "current").symlink_to(snapshot, target_is_directory=True)
        self.assertEqual(self.store.catalog_directory(), snapshot.resolve())
        self.store.reload_catalog()
        self.assertIn("90001:standard:0", self.store.catalog)
        self.assertEqual(self.store.waterfish_map["90001"]["song_id"], 90001)
        self.assertEqual(self.store.catalog["30:standard:0"]["constant"], 14.8)
        self.assertEqual(self.store.db_path.read_bytes(), personal_before)
        self.assertTrue(self.store.chart("30:standard:0")["is_ap"])
        self.assertFalse(self.store.chart("30:standard:0")["requires_venue"])
        self.assertEqual(self.store.chart("30:standard:0")["note"], "keep")
        self.assertTrue(self.store.chart("30:dx:0")["is_ap"])
        listing = self.client.get("/api/charts", params={"q": "90001"}).json()
        summary = self.client.get("/api/summary", params={"q": "90001"}).json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(summary["total"], 1)
        self.assertTrue(listing["items"][0]["requires_venue"])
        self.assertFalse(listing["items"][0]["in_original_table"])
        self.assertEqual(self.client.get("/api/summary").json()["total"], CHART_COUNT + 1)
        # A restart resolves the same pointer without reimporting the user's TSV.
        restarted = Store(runtime=self.tmp.name, catalog_dir=base)
        self.assertEqual(len(restarted.catalog), CHART_COUNT + 1)
        self.assertEqual(restarted.chart("30:standard:0")["note"], "keep")
        self.store.install_catalog(original)
        self.assertNotIn("90001:standard:0", self.store.catalog)
        self.assertTrue(self.store.chart("30:standard:0")["is_ap"])
        self.store.install_catalog(loaded)

    def test_bad_catalog_reload_keeps_loaded_snapshot(self):
        original = self.store._catalog_snapshot
        broken = Path(self.tmp.name) / "broken"
        broken.mkdir()
        (broken / "catalog.sqlite").write_bytes(b"not a sqlite file")
        self.store.catalog_root = broken
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.reload_catalog()
        self.assertIs(self.store._catalog_snapshot, original)

    def test_catalog_assets_follow_current_pointer_and_fall_back_to_base(self):
        base = Path(self.tmp.name) / "catalog-assets"
        snapshot = base / "snapshots" / "new"
        for directory in (base, snapshot):
            (directory / "jackets").mkdir(parents=True)
        (base / "jackets/30.png").write_bytes(b"base jacket")
        (base / "jackets/31.png").write_bytes(b"fallback jacket")
        (snapshot / "jackets/30.png").write_bytes(b"new jacket")
        (base / "catalog.json").write_text(json.dumps({"snapshot_at": "base"}))
        (snapshot / "catalog.json").write_text(json.dumps({"snapshot_at": "new"}))
        self.store.catalog_root = base
        self.assertEqual(self.client.get("/jackets/30.png").content, b"base jacket")
        self.assertEqual(self.client.get("/api/catalog/data").json()["snapshot_at"], "base")
        (base / "current").symlink_to(snapshot, target_is_directory=True)
        self.assertEqual(self.client.get("/jackets/30.png").content, b"new jacket")
        self.assertEqual(self.client.get("/jackets/31.png").content, b"fallback jacket")
        self.assertEqual(self.client.get("/api/catalog/data").json()["snapshot_at"], "new")
        self.assertEqual(self.client.get("/api/catalog/data").headers["cache-control"], "no-store")
        for filename in ("secret.json", "missing.png", "30.svg"):
            self.assertEqual(self.client.get(f"/jackets/{filename}").status_code, 404)

    def test_catalog_report_reads_only_the_active_snapshot(self):
        base = Path(self.tmp.name) / "catalog-report"
        snapshot = base / "snapshots" / "new"
        snapshot.mkdir(parents=True)
        self.store.catalog_root = base
        self.assertEqual(
            self.client.get("/api/catalog/update/report").json(),
            {"diff": {}, "issues": [], "sources": [], "source_policy": None},
        )
        (base / "catalog_diff.json").write_text(json.dumps({"snapshot": "old"}))
        (snapshot / "raw").mkdir()
        expected = {
            "diff": {"added_songs": [{"id": 90001}]},
            "issues": [{"kind": "missing_jacket", "song_id": 90001}],
            "sources": [{"file": "lxns_songs.json", "fetched_at": "2026-10-01T00:00:00Z"}],
            "source_policy": {
                "policy_version": "fixture-snapshot",
                "cn_catalog": {"authority": "lxns"},
            },
        }
        (snapshot / "catalog_diff.json").write_text(json.dumps(expected["diff"]))
        (snapshot / "build_report.json").write_text(
            json.dumps(
                {
                    "issues": expected["issues"],
                    "status": "ok",
                    "source_policy": expected["source_policy"],
                }
            )
        )
        (snapshot / "raw/sources.json").write_text(json.dumps(expected["sources"]))
        (base / "current").symlink_to(snapshot, target_is_directory=True)
        response = self.client.get("/api/catalog/update/report")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_legacy_manual_records_are_readable_but_http_writes_are_retired(self):
        key = "22:standard:4"
        self.store.save_manual(
            key,
            {
                "ap": True,
                "venue": "测试机厅",
                "achievement": 100.9876,
                "note": "历史记录",
                "expected_revision": 0,
            },
        )
        self.store.import_provider(
            {"records": [{"song_id": 22, "level_index": 4, "fc": "fc", "achievements": 100.2}]}
        )
        before = self.client.get(f"/api/charts/{key}").json()
        self.assertTrue(before["conflict"])
        self.assertEqual((before["venue"], before["achievement"]), ("测试机厅", 100.9876))
        for response in (
            self.client.put(f"/api/charts/{key}/manual", json={"ap": True}),
            self.client.delete(f"/api/charts/{key}/manual"),
        ):
            self.assertEqual(response.status_code, 410)
            self.assertEqual(response.json()["detail"]["code"], "manual_retired")
        self.assertEqual(self.client.get(f"/api/charts/{key}").json(), before)

    def test_venue_policy_is_exposed_and_low_claim_needs_no_venue(self):
        high, low = "22:standard:4", "30:standard:0"
        saved = self.put_record(high, json={"venue": "  测试机厅  "}).json()["chart"]
        self.assertTrue(saved["requires_venue"])
        self.assertEqual(saved["pending_record"]["venue"], "测试机厅")
        with self.store.db() as conn:
            events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        for venue in ("", " \t\u3000"):
            result = self.put_record(high, json={"venue": venue})
            self.assertEqual(result.status_code, 422)
            self.assertEqual(self.client.get(f"/api/charts/{high}").json(), saved)
        with self.store.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], events_before
            )
        low_result = self.put_record(low)
        self.assertEqual(low_result.status_code, 200)
        self.assertFalse(low_result.json()["chart"]["requires_venue"])
        self.assertEqual(low_result.json()["chart"]["pending_record"]["venue"], "")
        self.assertEqual(self.put_record("missing").status_code, 404)

    def test_catalog_only_venue_policy_uses_constant_threshold(self):
        template = self.store.catalog["30:standard:0"]
        for song_id, constant, required in ((90001, 12.5, False), (90002, 12.6, True)):
            key = f"{song_id}:standard:0"
            self.store.catalog[key] = template | {
                "song_id": song_id,
                "chart_key": key,
                "constant": constant,
            }
            chart = self.client.get(f"/api/charts/{key}").json()
            self.assertFalse(chart["in_original_table"])
            self.assertEqual(chart["requires_venue"], required)
            result = self.put_record(key)
            self.assertEqual(result.status_code, 422 if required else 200)

    def test_provider_fc_not_high_score_controls_ap(self):
        self.store.import_provider(
            {
                "records": [
                    {"song_id": 10030, "level_index": 0, "fc": "fc", "achievements": 100.5},
                    {"song_id": 30, "level_index": 0, "fc": "app", "achievements": 101},
                ]
            }
        )
        self.assertFalse(self.store.chart("30:dx:0")["is_ap"])
        self.assertTrue(self.store.chart("30:standard:0")["is_ap"])
        self.assertEqual(self.store.chart("30:standard:0")["status"], "app")
        # Remote values are distinct from user-confirmed cover score.
        self.assertIsNone(self.store.chart("30:standard:0")["achievement"])
        charts = self.client.get("/api/charts", params={"q": "10030", "group": "catalog"}).json()[
            "items"
        ]
        self.assertTrue(all(c["type"] == "dx" and c["song_id"] == 30 for c in charts))

    def test_sync_without_token_is_honest_and_secret_never_echoed(self):
        with patch.dict(os.environ, {"DIVINGFISH_IMPORT_TOKEN": ""}):
            self.assertEqual(self.client.post("/api/sync/divingfish").status_code, 409)
            value = "test-not-a-real-token"
            response = self.client.put("/api/settings/divingfish", json={"token": value})
            self.assertTrue(response.json()["configured"])
            self.assertNotIn(value, response.text)
            self.assertNotIn(value, self.client.get("/api/settings/divingfish").text)
            self.assertEqual(self.store.secret_path.stat().st_mode & 0o777, 0o600)
            invalid = self.client.put("/api/settings/divingfish", json={"token": value + "\n"})
            self.assertEqual(invalid.status_code, 422)
            self.assertNotIn(value, invalid.text)
            with patch("backend.app.httpx.get") as fetch:
                fetch.return_value.status_code = 200
                fetch.return_value.json.return_value = {
                    "records": [
                        {"song_id": 30, "level_index": 0, "fc": "ap", "achievements": 100.5}
                    ]
                }
                result = self.client.post("/api/sync/divingfish")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["matched"], 1)
                self.assertEqual(fetch.call_args.kwargs["headers"], {"Import-Token": value})
                self.assertFalse(fetch.call_args.kwargs["follow_redirects"])

    def test_failed_sync_keeps_existing_provider_snapshot(self):
        self.store.import_provider(
            {"records": [{"song_id": 30, "level_index": 0, "fc": "ap", "achievements": 100.5}]}
        )
        with self.assertRaises(ValueError):
            self.store.import_provider(
                {
                    "records": [
                        {"song_id": 30, "level_index": 0, "fc": "ap", "achievements": float("nan")}
                    ]
                }
            )
        self.assertTrue(self.store.chart("30:standard:0")["is_ap"])
        self.assertEqual(
            self.put_record("30:standard:0", json={"achievement": 101.5}).status_code,
            422,
        )

    def test_foreign_origin_cannot_mutate_and_data_is_uncached(self):
        result = self.put_record(
            "30:standard:0",
            headers={"Origin": "https://external.example"},
        )
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.client.get("/api/summary").headers["cache-control"], "no-store")
        self.assertEqual(
            self.client.get("/api/summary", headers={"Host": "evil.example"}).status_code, 400
        )


if __name__ == "__main__":
    unittest.main()
