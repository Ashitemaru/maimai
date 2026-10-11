import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.store import Store, canonical_chart_key


class ChartStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.catalog = self.root / "catalog"
        (self.catalog / "raw").mkdir(parents=True)
        chart = {
            "id": 10001,
            "difficulty": 3,
            "chart_key": "10001:3",
            "lxns_song_id": 1,
            "type": "dx",
            "title": "Example",
            "artist": "Artist",
            "difficulty_name": "MASTER",
            "constant": 13.3,
            "level": "13",
            "aliases": ["Nickname"],
            "jacket_path": "jackets/1.png",
            "external_ids": [{"namespace": "divingfish", "id": "10001", "type": "dx"}],
            "release_status": "resolved",
            "jp_release_version": "UNiVERSE",
            "admission_status": "admitted",
        }
        with closing(sqlite3.connect(self.catalog / "catalog.sqlite")) as conn, conn:
            conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value_json TEXT)")
            conn.execute("INSERT INTO metadata VALUES(?,?)", ("versions_cn", "[]"))
            conn.execute(
                "CREATE TABLE charts(id INTEGER,difficulty INTEGER,type TEXT,data_json TEXT,PRIMARY KEY(id,difficulty))"
            )
            conn.execute("INSERT INTO charts VALUES(?,?,?,?)", (10001, 3, "dx", json.dumps(chart)))
        raw = {
            "songs": [
                {
                    "id": 1,
                    "title": "Example",
                    "artist": "Artist",
                    "difficulties": {
                        "standard": [],
                        "dx": [
                            {"difficulty": 3, "level": "13", "level_value": 13.3},
                            {"difficulty": 4, "level": "14", "level_value": 14.0},
                        ],
                        "utage": [],
                    },
                }
            ]
        }
        (self.catalog / "raw/lxns_songs.json").write_text(json.dumps(raw))
        with closing(sqlite3.connect(self.runtime / "personal.sqlite3")) as conn, conn:
            conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO metadata VALUES(?,?)", ("import", "{}"))
        self.store = Store(runtime=self.runtime, catalog_dir=self.catalog)

    def tearDown(self):
        self.temp.cleanup()

    def test_flat_catalog_and_known_pending_chart(self):
        self.assertEqual(list(self.store.catalog), ["10001:3"])
        self.assertIn("10001:4", self.store.catalog.source_charts)
        chart = self.store.chart("1:dx:3")
        self.assertEqual((chart["id"], chart["difficulty"], chart["song_id"]), (10001, 3, 10001))
        self.assertEqual(chart["jacket_url"], "/jackets/1.png")
        self.assertIn("nickname", chart["_search"])
        self.assertIsNone(self.store.chart("10001:4"))
        self.assertEqual(self.store.meta("chart_key_schema"), 2)

    def test_personal_migration_preserves_all_values_and_pending_rows(self):
        with self.store.db() as conn:
            conn.execute("DELETE FROM metadata WHERE key='chart_key_schema'")
            conn.execute(
                "INSERT INTO legacy VALUES(?,?,?,?,?,?,?,?,?)",
                ("1:dx:4", "high", 14.0, 1, "机厅", "[ReM, DX] Example", "file.tsv", 2, 3),
            )
            conn.execute(
                "INSERT INTO manual VALUES(?,?,?,?,?,?,?)",
                ("1:dx:3", 1, "Arcade", 100.9, "Note", "2026-10-10", "then"),
            )
            conn.execute(
                "INSERT INTO provider VALUES(?,?,?,?,?)",
                ("1:dx:4", 101, "app", "then", '{"original":true}'),
            )
            conn.execute(
                "INSERT INTO events(chart_key,kind,created_at,data_json) VALUES(?,?,?,?)",
                ("1:dx:3", "manual", "then", '{"audit":true}'),
            )
            before = {
                table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                for table in ("legacy", "manual", "provider", "events")
            }
        self.store.reload_catalog()
        self.store.reload_catalog()
        with self.store.db() as conn:
            for table, old_rows in before.items():
                new_rows = [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                position = 1 if table == "events" else 0
                expected = []
                for row in old_rows:
                    row = list(row)
                    row[position] = canonical_chart_key(row[position])
                    expected.append(tuple(row))
                self.assertEqual(new_rows, expected)
        chart = self.store.chart("10001:3")
        self.assertTrue(chart["is_ap"])
        self.assertEqual(chart["venue"], "Arcade")
        self.assertEqual(chart["achievement"], 100.9)

    def test_collision_rolls_back_without_removing_data(self):
        with self.store.db() as conn:
            conn.execute("DELETE FROM metadata WHERE key='chart_key_schema'")
            for key in ("1:dx:3", "10001:3"):
                conn.execute(
                    "INSERT INTO provider VALUES(?,?,?,?,?)", (key, 100.9, "ap", "then", "{}")
                )
        with self.assertRaisesRegex(ValueError, "主键迁移有重复"):
            self.store.reload_catalog()
        with self.store.db() as conn:
            self.assertEqual(
                {row[0] for row in conn.execute("SELECT chart_key FROM provider")},
                {"1:dx:3", "10001:3"},
            )
        self.assertIsNone(self.store.meta("chart_key_schema"))

    def test_provider_keeps_known_pending_chart_without_admitting_it(self):
        result = self.store.import_provider(
            {
                "records": [
                    {
                        "song_id": 10001,
                        "level_index": 4,
                        "type": "DX",
                        "fc": "app",
                        "achievements": 101,
                    },
                ]
            }
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["unmatched"], 0)
        self.assertIsNone(self.store.chart("10001:4"))
        with self.store.db() as conn:
            record = conn.execute("SELECT * FROM provider").fetchone()
            self.assertEqual(record["chart_key"], "10001:4")
            self.assertEqual(record["fc"], "app")

    def test_conflicted_formal_record_is_rejected(self):
        with closing(sqlite3.connect(self.catalog / "catalog.sqlite")) as conn, conn:
            value = json.loads(conn.execute("SELECT data_json FROM charts").fetchone()[0])
            value["release_status"] = "conflict"
            conn.execute("UPDATE charts SET data_json=?", (json.dumps(value),))
        with self.assertRaisesRegex(ValueError, "正式曲库"):
            self.store.reload_catalog()

    def test_pending_admission_is_rejected_even_with_resolved_version(self):
        with closing(sqlite3.connect(self.catalog / "catalog.sqlite")) as conn, conn:
            value = json.loads(conn.execute("SELECT data_json FROM charts").fetchone()[0])
            value["admission_status"] = "pending"
            conn.execute("UPDATE charts SET data_json=?", (json.dumps(value),))
        with self.assertRaisesRegex(ValueError, "正式曲库"):
            self.store.reload_catalog()


if __name__ == "__main__":
    unittest.main()
