import unittest

from backend.divingfish_upload import personal_records, plan_upload, verify_records


def record(**values):
    return {
        "song_id": 10001,
        "level_index": 3,
        "title": "Example",
        "type": "DX",
        "achievements": 100.2,
        "fc": "ap",
        "fs": "fsp",
        "dxScore": 1200,
        **values,
    }


def indexed(*records):
    return personal_records({"records": list(records)})


class DivingFishUploadTests(unittest.TestCase):
    def test_remote_better_in_every_field_needs_no_write(self):
        before = indexed(record(achievements=101, fc="app", fs="fsdp", dxScore=1300))
        plan = plan_upload([record()], before)
        self.assertEqual(plan.body, [])
        self.assertEqual(plan.expected, before)
        self.assertEqual(plan.counts["unchanged_count"], 1)
        self.assertEqual(plan.counts["preserved_count"], 1)

    def test_independent_bests_merge_without_dropping_partial_improvement(self):
        before = indexed(record(achievements=100.9, fc="fc", fs="fsdp", dxScore=1200))
        local = record(achievements=100.8, fc="app", fs="sync", dxScore=1300)
        plan = plan_upload([local], before)
        self.assertEqual(plan.body, [record(achievements=100.9, fc="app", fs="fsdp", dxScore=1300)])
        self.assertEqual((plan.counts["improved_count"], plan.counts["preserved_count"]), (1, 1))
        self.assertEqual(local["achievements"], 100.8)
        self.assertEqual(before[10001, 3]["fc"], "fc")

    def test_only_changed_records_sent_and_derived_fields_ignored(self):
        old = record(rate="sss", ra=280)
        same = record(rate="sssp", ra=300)
        new = record(level_index=4)
        plan = plan_upload([same, new], indexed(old))
        self.assertEqual(plan.body, [new])
        self.assertEqual(plan.counts["new_count"], 1)
        self.assertEqual(plan.counts["unchanged_count"], 1)

    def test_record_identity_is_id_and_difficulty_not_title(self):
        before = indexed(record(), record(level_index=4), record(song_id=10002))
        changed = record(level_index=4, achievements=100.5, title="Renamed")
        plan = plan_upload([changed], before)
        self.assertEqual(plan.body, [changed])
        self.assertEqual(len(plan.expected), 3)
        self.assertEqual(plan.expected[10001, 3]["achievements"], 100.2)

    def test_decimal_achievement_changes_are_not_hidden_by_tolerance(self):
        plan = plan_upload([record(achievements=100.2001)], indexed(record()))
        self.assertEqual(len(plan.body), 1)
        failures = verify_records(plan.expected, indexed(record()))
        self.assertEqual(failures[0]["fields"], ["achievements"])

    def test_outside_records_include_deleted_songs_and_native_utage(self):
        old = record(song_id=9999)
        utage = record(song_id=111359, level_index=10, achievements=200.1234)
        before = indexed(old, utage)
        plan = plan_upload([record()], before)
        self.assertEqual(len(plan.body), 1)
        self.assertEqual(plan.counts["outside_upload_count"], 2)
        self.assertEqual(verify_records(plan.expected, indexed(old, utage, record())), [])
        self.assertEqual(
            verify_records(plan.expected, indexed(record())),
            [
                {"song_id": 9999, "level_index": 3, "fields": ["record"]},
                {"song_id": 111359, "level_index": 10, "fields": ["record"]},
            ],
        )

    def test_postread_allows_newer_improvements_and_new_outside_records(self):
        expected = indexed(record())
        actual = indexed(record(achievements=100.3, fc="app"), record(song_id=10002))
        self.assertEqual(verify_records(expected, actual), [])

    def test_postread_reports_every_degraded_field(self):
        expected = indexed(record())
        actual = indexed(record(achievements=100.1, fc="fc", fs="sync", dxScore=1199))
        self.assertEqual(
            verify_records(expected, actual)[0]["fields"], ["achievements", "fc", "fs", "dxScore"]
        )

    def test_malformed_personal_records_do_not_get_silently_skipped(self):
        malformed = [
            None,
            {},
            {"records": None},
            {"records": [record(), record()]},
            {"records": [record(song_id=True)]},
            {"records": [record(level_index=True)]},
            {"records": [record(level_index=-1)]},
            {"records": [record(achievements=float("nan"))]},
            {"records": [record(achievements=True)]},
            {"records": [record(dxScore=None)]},
            {"records": [record(fc="unknown")]},
            {"records": [record(fs="unknown")]},
            {"records": ["not a score"]},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises((KeyError, ValueError)):
                    personal_records(payload)

    def test_duplicate_candidate_fails_before_any_write(self):
        with self.assertRaises(ValueError):
            plan_upload([record(), record()], {})

    def test_invalid_remote_best_is_not_sent_where_the_api_would_clamp_it(self):
        with self.assertRaises(ValueError):
            plan_upload([record(fc="app")], indexed(record(achievements=102)))


if __name__ == "__main__":
    unittest.main()
