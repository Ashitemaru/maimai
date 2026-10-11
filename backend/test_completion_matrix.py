"""Matrix aggregation and drilldown checks with an isolated personal database."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.store import ChartIndex, Store
from backend.test_app import FakeUpdater
from tests.support import ROOT, create_app

OLD_VERSIONS = [
    (10000, "maimai", "初"),
    (11000, "maimai PLUS", "真"),
    (12000, "GreeN", "超"),
    (13000, "GreeN PLUS", "檄"),
    (14000, "ORANGE", "橙"),
    (15000, "ORANGE PLUS", "晓"),
    (16000, "PiNK", "桃"),
    (17000, "PiNK PLUS", "樱"),
    (18000, "MURASAKi", "紫"),
    (18500, "MURASAKi PLUS", "堇"),
    (19000, "MiLK", "白"),
    (19500, "MiLK PLUS", "雪"),
    (19900, "FiNALE", "辉"),
]
DX_VERSIONS = [
    (20000, "舞萌DX", "熊华"),
    (21000, "舞萌DX 2021", "爽煌"),
    (22000, "舞萌DX 2022", "宙星"),
    (23000, "舞萌DX 2023", "祭祝"),
    (24000, "舞萌DX 2024", "双宴"),
    (25000, "舞萌DX 2025", "镜"),
    (25500, "舞萌DX 2026", "彩"),
]
VERSIONS = [
    {"version": version, "title": title} for version, title, _ in [*OLD_VERSIONS, *DX_VERSIONS]
]


class CompletionMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        catalog_dir = Path(self.tmp.name) / "catalog"
        catalog_dir.mkdir()
        shutil.copy2(ROOT / "data/catalog/catalog.sqlite", catalog_dir / "catalog.sqlite")
        self.store = Store(runtime=self.tmp.name, catalog_dir=catalog_dir)
        self.base = self.store.catalog["22:standard:4"]
        self.client = TestClient(create_app(self.store, updater=FakeUpdater()))

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def install(self, specs, versions=VERSIONS):
        catalog = ChartIndex(versions_cn=versions)
        for index, spec in enumerate(specs):
            song_id = spec.get("song_id", 90001 + index)
            difficulty = spec.get("difficulty_index", 3)
            kind = spec.get("type", "standard")
            key = f"{song_id}:{kind}:{difficulty}"
            catalog[key] = dict(
                self.base,
                chart_key=key,
                song_id=song_id,
                type=kind,
                title=f"Matrix {song_id}",
                _search=f"matrix {song_id}",
                constant=spec.get("constant", 13.3),
                difficulty_index=difficulty,
                api_version=spec.get("api_version", 20000),
                cn_version=spec.get("cn_version", spec.get("api_version", 20000)),
                jp_release_version=spec.get("jp_release_version", "PRiSM"),
            )
        self.store.install_catalog((catalog, {}))
        return list(catalog)

    def get_matrix(self, params=None):
        response = self.client.get("/api/completion-matrix", params=params or {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_conservation(self, matrix):
        keys = [version["value"] for version in matrix["versions"]]
        self.assertEqual(list(matrix["totals"]), keys)
        for row in matrix["rows"]:
            self.assertEqual(list(row["cells"]), keys)
            self.assertGreater(row["total"]["total"], 0)
            for name in ("complete", "total", "remaining"):
                self.assertEqual(
                    row["total"][name], sum(cell[name] for cell in row["cells"].values())
                )
        for key in keys:
            for name in ("complete", "total", "remaining"):
                self.assertEqual(
                    matrix["totals"][key][name],
                    sum(row["cells"][key][name] for row in matrix["rows"]),
                )
        for name in ("complete", "total", "remaining"):
            self.assertEqual(
                matrix["summary"][name], sum(counts[name] for counts in matrix["totals"].values())
            )
            self.assertEqual(
                matrix["summary"][name], sum(row["total"][name] for row in matrix["rows"])
            )
        counts = [matrix["summary"], *matrix["totals"].values()]
        for row in matrix["rows"]:
            counts.extend([row["total"], *row["cells"].values()])
        for value in counts:
            self.assertEqual(value["complete"] + value["remaining"], value["total"])

    def test_all_cn_versions_use_adjacent_metadata_boundaries_and_short_labels(self):
        specs = []
        expected = {}
        for index, row in enumerate(VERSIONS):
            stop = VERSIONS[index + 1]["version"] if index + 1 < len(VERSIONS) else 26000
            specs.extend([{"cn_version": row["version"]}, {"cn_version": stop - 1}])
            expected[str(row["version"])] = {"complete": 0, "total": 2, "remaining": 2}
        # Future, invalid, and missing raw CN values stay unknown even with a known JP version.
        specs.extend({"cn_version": value} for value in (26000, 9999, None, True))
        self.install(specs)
        matrix = self.get_matrix()
        expected_versions = [
            *[
                {"value": str(version), "label": label, "title": title}
                for version, title, label in OLD_VERSIONS
            ],
            *[
                {"value": str(version), "label": label, "title": title}
                for version, title, label in DX_VERSIONS
            ],
            {"value": "__unknown__", "label": "未知", "title": "国服版本待核实"},
        ]
        self.assertEqual(matrix["versions"], expected_versions)
        self.assertEqual(
            matrix["totals"],
            expected | {"__unknown__": {"complete": 0, "total": 4, "remaining": 4}},
        )
        filters = self.client.get("/api/chart-filters").json()
        self.assertEqual(filters["cn_matrix_versions"], expected_versions[:-1])
        self.assertEqual(
            filters["cn_versions"],
            [
                *[{"value": str(version), "label": title} for version, title, _ in OLD_VERSIONS],
                {"value": "20000", "label": "DX"},
                *[
                    {"value": str(version), "label": "DX" + title[-4:]}
                    for version, title, _ in DX_VERSIONS[1:]
                ],
            ],
        )
        self.assert_conservation(matrix)

    def test_completion_matches_table_including_legacy_provider_and_manual_corrections(self):
        keys = self.install(
            [
                {"cn_version": 10000, "constant": 12.0},
                {"cn_version": 18504, "constant": 14.9},
                {"cn_version": 25504, "constant": 14.9, "type": "dx"},
                {"cn_version": None, "constant": 13.3},
                {"cn_version": 19500, "constant": 13.3},
            ]
        )
        with self.store.db() as conn:
            for key in (keys[0], keys[4]):
                conn.execute(
                    "INSERT INTO legacy VALUES (?, 'high', 13.3, 1, '', '', 'fixture', 1, 1)",
                    (key,),
                )
            for key, ap in ((keys[1], 1), (keys[4], 0)):
                conn.execute(
                    "INSERT INTO manual VALUES (?, ?, 'Fixture arcade', NULL, '', NULL, 'fixture')",
                    (key, ap),
                )
            for key in (keys[2], keys[4]):
                conn.execute(
                    "INSERT INTO provider VALUES (?, 100.5, 'ap', 'fixture', '{}')", (key,)
                )
        matrix = self.get_matrix()
        table = self.client.get("/api/completion-table").json()
        self.assertEqual(matrix["summary"], {"complete": 3, "total": 5, "remaining": 2})
        self.assertEqual(matrix["summary"]["complete"], table["summary"]["complete"])
        self.assertEqual(matrix["summary"]["total"], table["total"])
        self.assertEqual([row["constant"] for row in matrix["rows"]], [14.9, 13.3, 12.0])
        self.assertEqual(
            matrix["totals"]["10000"]["complete"], 1
        )  # Legacy completion, no confirmed AP.
        self.assertEqual(
            matrix["totals"]["19500"]["complete"], 0
        )  # Explicit false overrides both sources.
        self.assert_conservation(matrix)

    def test_cn_and_jp_releases_are_independent_and_each_chart_can_drill_down(self):
        keys = self.install(
            [
                {
                    "song_id": 90001,
                    "cn_version": 10004,
                    "jp_release_version": "PRiSM",
                    "constant": 12.0,
                },
                {
                    "song_id": 90001,
                    "difficulty_index": 4,
                    "cn_version": 25504,
                    "jp_release_version": "maimai",
                },
                {"cn_version": 18504, "jp_release_version": "DX", "constant": 12.0},
                {"cn_version": None, "jp_release_version": "maimai"},
            ]
        )
        matrix = self.get_matrix()
        self.assertEqual(matrix["totals"]["10000"]["total"], 1)
        self.assertEqual(matrix["totals"]["18500"]["total"], 1)
        self.assertEqual(matrix["totals"]["25500"]["total"], 1)
        self.assertEqual(matrix["totals"]["__unknown__"]["total"], 1)
        for difficulty in (None, 3, 4):
            params = {} if difficulty is None else {"difficulty": difficulty}
            result = self.get_matrix(params)
            for row in result["rows"]:
                for version, counts in row["cells"].items():
                    filters = params | {
                        "cn_version": version,
                        "constant_min": row["constant"],
                        "constant_max": row["constant"],
                    }
                    listing = self.client.get("/api/charts", params=filters).json()
                    table = self.client.get("/api/completion-table", params=filters).json()
                    summary = self.client.get("/api/summary", params=filters).json()
                    self.assertEqual(listing["total"], counts["total"])
                    self.assertEqual(table["total"], counts["total"])
                    self.assertEqual(summary["complete"], counts["complete"])
        unknown = self.client.get("/api/charts", params={"cn_version": "__unknown__"}).json()
        self.assertEqual([chart["chart_key"] for chart in unknown["items"]], [keys[3]])
        self.assert_conservation(matrix)

    def test_difficulty_constant_and_cn_ranges_filter_without_using_personal_state(self):
        self.install(
            [
                {"cn_version": 10000, "difficulty_index": 0, "constant": 5.0},
                {"cn_version": 18500, "difficulty_index": 3, "constant": 13.3},
                {"cn_version": 25504, "difficulty_index": 3, "constant": 13.9},
                {"cn_version": 25504, "difficulty_index": 4, "constant": 14.9},
                {"cn_version": None, "difficulty_index": 3, "constant": 13.5},
            ]
        )
        for params, total in (
            ({"difficulty": 0}, 1),
            ({"difficulty": 3}, 3),
            ({"difficulty": 4}, 1),
            ({"constant_min": 13.3, "constant_max": 13.9}, 3),
            ({"cn_version_min": 18500}, 3),
            ({"cn_version_max": 18500}, 2),
            (
                {
                    "cn_version_min": 18500,
                    "cn_version_max": 25500,
                    "difficulty": 3,
                    "constant_max": 13.9,
                },
                2,
            ),
        ):
            with self.subTest(params=params):
                matrix = self.get_matrix(params)
                self.assertEqual(matrix["summary"]["total"], total)
                if "cn_version_min" in params or "cn_version_max" in params:
                    self.assertNotIn("__unknown__", matrix["totals"])
                self.assert_conservation(matrix)
        self.assertEqual(self.get_matrix({"status": "complete"}), self.get_matrix())

    def test_empty_rows_zero_columns_and_unknown_visibility(self):
        self.install([{"cn_version": 25504, "difficulty_index": 4}])
        populated = self.get_matrix()
        self.assertNotIn("__unknown__", populated["totals"])
        self.assertEqual(populated["totals"]["10000"], {"complete": 0, "total": 0, "remaining": 0})
        for params in ({"difficulty": 0}, {"constant_min": 15}, {"cn_version_min": 27000}):
            with self.subTest(params=params):
                matrix = self.get_matrix(params)
                self.assertEqual(matrix["rows"], [])
                self.assertEqual(matrix["summary"], {"complete": 0, "total": 0, "remaining": 0})
                self.assertNotIn("__unknown__", matrix["totals"])
                self.assert_conservation(matrix)
        self.install([])
        self.assertEqual(self.get_matrix()["rows"], [])
        self.install([{"cn_version": 20000}], versions=[])
        unknown = self.get_matrix()
        self.assertEqual([item["value"] for item in unknown["versions"]], ["__unknown__"])
        self.assertEqual(unknown["totals"]["__unknown__"]["total"], 1)

    def test_matrix_metadata_and_charts_come_from_one_published_snapshot(self):
        self.install([{"cn_version": 18504}])
        captured = self.store.all_charts()
        original = self.get_matrix()
        self.install([{"cn_version": 25504}], versions=VERSIONS[-1:])
        self.assertEqual([version["value"] for version in self.get_matrix()["versions"]], ["25500"])
        with patch.object(self.store, "all_charts", return_value=captured):
            self.assertEqual(self.get_matrix(), original)

    def test_invalid_queries_are_rejected_without_breaking_existing_dx_filters(self):
        self.install([{"cn_version": 25504}])
        for params in (
            {"difficulty": -1},
            {"difficulty": 5},
            {"difficulty": "MASTER"},
            {"constant_min": "NaN"},
            {"constant_max": "Infinity"},
            {"constant_min": 14, "constant_max": 13},
            {"cn_version_min": 25500, "cn_version_max": 20000},
            {"cn_version_min": 9999},
            {"cn_version_max": "__unknown__"},
        ):
            with self.subTest(params=params):
                self.assertEqual(
                    self.client.get("/api/completion-matrix", params=params).status_code, 422
                )
        for value in (9999, "DX2026", "__invalid__", "25504.5", "true"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.client.get("/api/charts", params={"cn_version": value}).status_code, 422
                )
        for value, expected in ((25500, 1), (20000, 0), (20100, 0), (10000, 0)):
            result = self.client.get("/api/charts", params={"cn_version": value})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["total"], expected)


if __name__ == "__main__":
    unittest.main()
