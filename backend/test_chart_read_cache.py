"""Snapshot reuse must never weaken AP transaction or publication semantics."""

import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

from backend.ap_records import RecordConflictError
from backend.store import ChartIndex
from tests import test_chart_store


class ChartReadCacheTests(unittest.TestCase):
    setUp = test_chart_store.ChartStoreTests.setUp
    key = "10001:3"

    def tearDown(self):
        self.store.close()
        test_chart_store.ChartStoreTests.tearDown(self)

    def save(self, *, revision=0, request_id="fixture", venue="测试机厅"):
        with self.store.db() as conn:
            conn.execute("INSERT OR IGNORE INTO venues VALUES(?,?,?)", (venue, "fixture", ""))
        return self.store.save_ap_record(
            self.key,
            {
                "expected_revision": revision,
                "request_id": request_id,
                "venue": venue,
            },
        )

    def score_payload(self):
        return {
            "records": [
                {
                    "song_id": 10001,
                    "level_index": 3,
                    "type": "DX",
                    "fc": "ap",
                    "achievements": 100.9,
                }
            ]
        }

    def test_simultaneous_cold_reads_assemble_once_and_return_owned_rows(self):
        barrier = threading.Barrier(8)

        def read(_):
            barrier.wait(timeout=5)
            return self.store.all_charts()

        with patch.object(
            self.store, "_assemble_charts", wraps=self.store._assemble_charts
        ) as build:
            with ThreadPoolExecutor(max_workers=8) as pool:
                reads = list(pool.map(read, range(8)))
            self.assertEqual(build.call_count, 1)
        self.assertEqual(len({id(rows) for rows in reads}), 8)
        self.assertEqual(len({id(rows[0]) for rows in reads}), 8)
        self.assertEqual(len({id(rows[0]["warnings"]) for rows in reads}), 8)

    def test_nested_consumer_mutations_do_not_poison_shared_state(self):
        self.save()
        first = self.store.all_charts()
        first[0]["title"] = "incorrect"
        first[0]["pending_record"]["venue"] = "incorrect"
        first[0]["warnings"].append("incorrect")
        first.cn_versions.append({"value": "incorrect"})
        single = self.store.chart(self.key)
        self.assertEqual(single["title"], "Example")
        self.assertEqual(single["pending_record"]["venue"], "测试机厅")
        self.assertNotIn("incorrect", single["warnings"])
        single["pending_record"]["reason"] = "incorrect"
        second = self.store.all_charts()
        self.assertEqual(second.cn_versions, [])
        self.assertEqual(second[0]["pending_record"]["reason"], "待同步核实")

    def test_personal_save_edit_cancel_and_sync_invalidate_without_a_timer(self):
        self.assertIsNone(self.store.chart(self.key)["pending_record"])
        first = self.save()
        self.assertEqual(self.store.all_charts()[0]["pending_record"]["venue"], "测试机厅")
        second = self.save(revision=first["record_revision"], request_id="edit", venue="新测试机厅")
        self.assertEqual(self.store.chart(self.key)["pending_record"]["venue"], "新测试机厅")
        self.store.cancel_ap_record(self.key, second["record_revision"])
        self.assertIsNone(self.store.chart(self.key)["pending_record"])
        self.store.import_provider(self.score_payload())
        self.assertTrue(self.store.chart(self.key)["is_ap"])

    def test_external_database_commit_invalidates_read_cache(self):
        self.assertFalse(self.store.chart(self.key)["is_ap"])
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute(
                "INSERT INTO provider VALUES(?,?,?,?,?)", (self.key, 100.9, "ap", "fixture", "{}")
            )
        self.assertTrue(self.store.chart(self.key)["is_ap"])

    def test_database_replacement_reopens_change_observer(self):
        self.assertFalse(self.store.chart(self.key)["is_ap"])
        replacement = self.runtime / "replacement.sqlite3"
        source = sqlite3.connect(self.store.db_path)
        destination = sqlite3.connect(replacement)
        try:
            source.backup(destination)
            destination.execute(
                "INSERT INTO provider VALUES(?,?,?,?,?)", (self.key, 100.9, "ap", "fixture", "{}")
            )
            destination.commit()
        finally:
            source.close()
            destination.close()
        replacement.replace(self.store.db_path)
        self.assertTrue(self.store.chart(self.key)["is_ap"])

    def test_commit_during_build_is_not_cached_under_a_newer_version(self):
        original = self.store._assemble_charts
        calls = 0

        def build(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                with sqlite3.connect(self.store.db_path) as conn:
                    conn.execute(
                        "INSERT INTO provider VALUES(?,?,?,?,?)",
                        (self.key, 100.9, "ap", "fixture", "{}"),
                    )
            return result

        with patch.object(self.store, "_assemble_charts", side_effect=build):
            self.assertFalse(self.store.chart(self.key)["is_ap"])
            self.assertTrue(self.store.chart(self.key)["is_ap"])
            self.assertEqual(calls, 2)
            self.assertTrue(self.store.chart(self.key)["is_ap"])
            self.assertEqual(calls, 2)

    def test_transaction_reads_bypass_cache_and_rollback_cannot_leak(self):
        self.assertFalse(self.store.chart(self.key)["is_ap"])
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO provider VALUES(?,?,?,?,?)",
                    (self.key, 100.9, "ap", "fixture", "{}"),
                )
                self.assertTrue(self.store.all_charts(connection=conn)[0]["is_ap"])
                raise RuntimeError("rollback")
        self.assertFalse(self.store.chart(self.key)["is_ap"])
        self.save()
        result = self.store.import_provider(self.score_payload())
        self.assertEqual(
            (result["confirmed"], result["pending"], result["venue_pending"]), (1, 0, 0)
        )
        self.assertEqual(self.store.meta("last_sync"), result)

    def test_writer_transaction_read_does_not_wait_for_reader_cache_lock(self):
        read_entered, release_read, transaction_read = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        original = self.store._assemble_charts

        def slow_read(*args, **kwargs):
            if kwargs.get("connection") is None:
                read_entered.set()
                if not release_read.wait(timeout=5):
                    raise AssertionError("read was not released")
            return original(*args, **kwargs)

        def write():
            with self.store.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO provider VALUES(?,?,?,?,?)",
                    (self.key, 100.9, "ap", "fixture", "{}"),
                )
                self.assertTrue(self.store.all_charts(connection=conn)[0]["is_ap"])
                transaction_read.set()

        with patch.object(self.store, "_assemble_charts", side_effect=slow_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reader = pool.submit(self.store.all_charts)
                self.assertTrue(read_entered.wait(timeout=5))
                writer = pool.submit(write)
                try:
                    self.assertTrue(transaction_read.wait(timeout=5))
                finally:
                    release_read.set()
                writer.result(timeout=5)
                reader.result(timeout=5)
        self.assertTrue(self.store.chart(self.key)["is_ap"])

    def test_catalog_publication_racing_with_read_invalidates_old_snapshot(self):
        entered, release, publishing = threading.Event(), threading.Event(), threading.Event()
        original = self.store._assemble_charts
        replacement = ChartIndex(
            {self.key: {**self.store.catalog[self.key], "title": "Published"}}, schema_version=2
        )

        def slow_build(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("read was not released")
            return result

        def publish():
            publishing.set()
            self.store.install_catalog((replacement, self.store.waterfish_map))

        with patch.object(self.store, "_assemble_charts", side_effect=slow_build):
            with ThreadPoolExecutor(max_workers=2) as pool:
                read = pool.submit(self.store.all_charts)
                self.assertTrue(entered.wait(timeout=5))
                writer = pool.submit(publish)
                self.assertTrue(publishing.wait(timeout=5))
                release.set()
                self.assertEqual(read.result(timeout=5)[0]["title"], "Example")
                writer.result(timeout=5)
        self.assertEqual(self.store.chart(self.key)["title"], "Published")

    def test_empty_queue_does_not_read_or_assemble_chart_catalog(self):
        with patch.object(
            self.store, "all_charts", side_effect=AssertionError("unexpected full read")
        ):
            self.assertEqual(self.store.ap_records(), {"items": [], "count": 0})

    def test_queue_metadata_keeps_revision_of_its_pending_row_during_concurrent_edit(self):
        first = self.save()
        original_db = self.store.db
        edited = False

        @contextmanager
        def interleaved_read():
            nonlocal edited
            with original_db() as connection:
                yield connection
            if not edited:
                edited = True
                self.save(
                    revision=first["record_revision"], request_id="newer", venue="另一测试机厅"
                )

        with patch.object(self.store, "db", side_effect=interleaved_read):
            item = self.store.ap_records()["items"][0]
        self.assertEqual(item["pending_record"]["venue"], "测试机厅")
        self.assertEqual(item["record_revision"], item["pending_record"]["revision"])
        self.assertLess(item["record_revision"], self.store.chart(self.key)["record_revision"])
        with self.assertRaises(RecordConflictError):
            self.save(revision=item["record_revision"], request_id="stale", venue="stale")

    def test_nonempty_queue_also_avoids_full_personal_chart_assembly(self):
        self.save()
        with patch.object(
            self.store, "all_charts", side_effect=AssertionError("unexpected full read")
        ):
            result = self.store.ap_records()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["title"], "Example")

    def test_ordinary_projection_does_not_read_large_raw_evidence(self):
        self.save()
        self.store.import_provider(self.score_payload())
        with self.store.db() as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            self.store.all_charts(connection=conn)
        selects = "\n".join(
            statement.lower()
            for statement in statements
            if statement.lstrip().lower().startswith("select")
        )
        self.assertNotIn("raw_json", selects)
        self.assertNotIn("evidence_json", selects)
        self.assertNotIn("chart_json", selects)
        self.assertNotIn("select * from provider", selects)

    def test_close_releases_observer_and_next_read_can_reopen(self):
        self.store.all_charts()
        connection = self.store._version_connection
        self.store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("PRAGMA data_version")
        self.assertEqual(self.store.chart(self.key)["title"], "Example")


if __name__ == "__main__":
    unittest.main()
