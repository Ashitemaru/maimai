"""The HTTP API over a generated catalog with synthetic legacy imports."""

import json
import os
import shutil
import sqlite3
from unittest.mock import patch

from backend.catalog_decisions import StaleDecisionError
from backend.catalog_updates import BusyError
from backend.chart_query import selection_summary
from backend.store import normalized
from tests.fixtures.catalog import CHART_COUNT, HIGH_COMPLETE, IMPORTED_COUNT, LEGACY_COMPLETE
from tests.support import FOREIGN, FixtureAppCase


class AppTests(FixtureAppCase):
    def fixture_chart(self, song_id, **changes):
        chart = {
            **self.store.chart("22:4"),
            "song_id": song_id,
            "title": f"Test {song_id}",
            "type": "standard",
            "difficulty_index": 3,
            "constant": 13.3,
            "level": "13",
            "group": "high",
            "is_complete": False,
            "is_ap": False,
            "status": "incomplete",
            "venue": "",
            "jp_release_version": "PiNK",
            **changes,
        }
        chart["chart_key"] = f"{song_id}:{chart['type']}:{chart['difficulty_index']}"
        chart["_search"] = normalized(chart["title"])
        return chart

    def test_legacy_import_is_reconciled_and_distinct_from_ap(self):
        data = self.client.get("/api/summary").json()
        self.assertEqual(
            (data["total"], data["complete"], data["ap"]), (CHART_COUNT, LEGACY_COMPLETE, 0)
        )
        imported = self.store.meta("import")
        self.assertEqual(imported["total"], IMPORTED_COUNT)
        self.assertEqual(imported["groups"]["high"]["complete"], HIGH_COMPLETE)
        with self.store.db() as conn:
            labels = dict(conn.execute("SELECT chart_key,label FROM legacy"))
        self.assertEqual(labels["383:3"], "[MAS, SD] Link(CoF)")  # Same title, other song.
        self.assertEqual(self.store.chart("11422:0")["display_title"], "（空白曲名）")

    def test_listing_and_summary_apply_the_same_filters(self):
        base = {"title": "Shared title", "type": "dx", "venue": "Test arcade", "is_complete": True}
        exclusions = [
            {"title": "Other title"},
            {"group": "low"},
            {"type": "standard"},
            {"is_complete": False},
            {"constant": 13.4},
            {"venue": "Other arcade"},
            {"jp_release_version": "PiNK PLUS"},
        ]
        fixtures = [self.fixture_chart(1, **base)]
        fixtures += [
            self.fixture_chart(i + 2, **base | change) for i, change in enumerate(exclusions)
        ]
        params = {
            "q": "shared",
            "group": "high",
            "type": "dx",
            "status": "complete",
            "constant_min": 13.3,
            "constant_max": 13.3,
            "venue": "Test",
            "version": "PiNK",
        }
        with patch.object(self.store, "all_charts", return_value=fixtures):
            listing = self.client.get("/api/charts", params=params).json()
            summary = self.client.get("/api/summary", params=params).json()
        self.assertEqual([c["song_id"] for c in listing["items"]], [1])
        self.assertEqual(summary, selection_summary(listing["items"], summary["sync"]))

    def test_catalog_reload_publishes_new_charts_and_keeps_personal_records(self):
        self.store.import_provider(
            {"records": [{"song_id": 30, "level_index": 0, "fc": "ap", "achievements": 100.6}]}
        )
        personal = self.store.db_path.read_bytes()
        snapshot = self.catalog_dir / "snapshots/new"
        snapshot.mkdir(parents=True)
        shutil.copy2(self.catalog_dir / "catalog.sqlite", snapshot / "catalog.sqlite")
        with sqlite3.connect(snapshot / "catalog.sqlite") as conn:
            chart = json.loads(
                conn.execute(
                    "SELECT data_json FROM charts WHERE id=30 AND difficulty=0"
                ).fetchone()[0]
            )
            chart.update(id=90001, title="New chart", divingfish_id="90001", external_ids=[])
            conn.execute(
                "INSERT INTO charts VALUES (?,?,?,?)", (90001, 0, "standard", json.dumps(chart))
            )
            conn.execute(
                "UPDATE charts SET data_json=json_set(data_json,'$.constant',14.8) WHERE id=30 AND difficulty=0"
            )
        (self.catalog_dir / "current").symlink_to(snapshot, target_is_directory=True)
        self.store.reload_catalog()
        self.assertEqual(self.store.catalog["30:0"]["constant"], 14.8)
        self.assertTrue(self.store.chart("30:0")["is_ap"])
        self.assertEqual(self.store.db_path.read_bytes(), personal)
        self.assertEqual(self.client.get("/api/summary").json()["total"], CHART_COUNT + 1)
        self.assertEqual(len(self.new_store().catalog), CHART_COUNT + 1)  # Restart follows pointer.

        original = self.store._catalog_snapshot
        (snapshot / "catalog.sqlite").write_bytes(b"not a sqlite file")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.reload_catalog()
        self.assertIs(self.store._catalog_snapshot, original)

    def test_record_decision_api_validates_payload_errors_and_origin(self):
        path = "/api/catalog/records/decisions/batch"
        record = {"id": 383, "difficulty": 1, "choices": [{"field_key": "title", "option_id": "a"}]}
        payload = {"base_catalog_revision": "r1", "base_decision_revision": 0, "records": [record]}
        self.assertEqual(self.client.post(path, json=payload).status_code, 202)
        self.assertEqual(self.updater.last_records["records"], [record | {"note": ""}])
        for wrong in (
            payload | {"records": []},
            payload | {"records": [record | {"difficulty": 5}]},
            payload | {"records": [record | {"choices": [{"field_key": "title", "value": "x"}]}]},
        ):
            self.assertEqual(self.client.post(path, json=wrong).status_code, 422)
        for error, code in (
            (StaleDecisionError("stale"), 409),
            (BusyError("busy"), 409),
            (ValueError("no"), 422),
        ):
            self.updater.error = error
            self.assertEqual(self.client.post(path, json=payload).status_code, code)
        self.updater.error = None
        self.assertEqual(self.client.post(path, json=payload, headers=FOREIGN).status_code, 403)
        revoke = {
            "base_catalog_revision": "r1",
            "base_decision_revision": 0,
            "id": 383,
            "difficulty": 1,
        }
        revoke_path = "/api/catalog/records/decisions/revoke"
        self.assertEqual(
            self.client.post(revoke_path, json=revoke | {"field_keys": []}).status_code, 422
        )
        self.assertEqual(self.client.post(revoke_path, json=revoke).status_code, 202)
        self.assertEqual(self.updater.calls, ["record-batch", "record-revoke"])

    def test_token_is_never_echoed_and_stored_private(self):
        with patch.dict(os.environ, {"DIVINGFISH_IMPORT_TOKEN": ""}):
            value = "test-not-a-real-token"
            response = self.client.put("/api/settings/divingfish", json={"token": value})
            self.assertTrue(response.json()["configured"])
            self.assertNotIn(
                value, response.text + self.client.get("/api/settings/divingfish").text
            )
            self.assertEqual(self.store.secret_path.stat().st_mode & 0o777, 0o600)

    def test_foreign_origin_or_host_is_rejected_and_data_is_uncached(self):
        body = {"expected_revision": 0, "request_id": "x", "venue": ""}
        self.assertEqual(
            self.client.put("/api/charts/30:0/ap-record", json=body, headers=FOREIGN).status_code,
            403,
        )
        self.assertEqual(self.client.get("/api/summary").headers["cache-control"], "no-store")
        self.assertEqual(
            self.client.get("/api/summary", headers={"Host": "evil.example"}).status_code, 400
        )

    def test_readiness_fails_when_a_database_is_missing(self):
        with patch.dict(os.environ, {"MAIMAI_RELEASE_SHA": "synthetic-release"}):
            ready = self.client.get("/api/ready").json()
            self.assertEqual(ready["release_sha"], "synthetic-release")
            self.assertEqual(ready["checks"], {"catalog": True, "personal": True})
            for name, path in (
                ("catalog", self.catalog_dir / "catalog.sqlite"),
                ("personal", self.store.db_path),
            ):
                hidden = path.with_suffix(".hidden")
                path.replace(hidden)
                try:
                    response = self.client.get("/api/ready")
                    self.assertEqual(
                        (response.status_code, response.json()["checks"][name]), (503, False)
                    )
                finally:
                    hidden.replace(path)
