import unittest
from copy import deepcopy

from scripts.catalog_versions import object_fingerprint, resolve_versions, validate_decision


def lxns(title="Song", artist="Artist", sid=1, both=False):
    chart = {"type": "standard", "difficulty": 3, "level_value": 13.0, "version": 99999}
    difficulties = {"standard": [chart, dict(chart, difficulty=4)], "dx": []}
    if both:
        difficulties["dx"] = [dict(chart, type="dx")]
    return {
        "songs": [
            {
                "id": sid,
                "title": title,
                "artist": artist,
                "version": 99999,
                "difficulties": difficulties,
            }
        ]
    }


def arcade(title="Song", artist="Artist", version="ORANGE", external_id="external", kind="std"):
    return {
        "songs": [
            {
                "songId": external_id,
                "title": title,
                "artist": artist,
                "version": "WRONG TOP LEVEL",
                "releaseDate": "1900-01-01",
                "sheets": [
                    {
                        "type": kind,
                        "difficulty": difficulty,
                        "version": version,
                        "regions": {"jp": False, "cn": True},
                        "regionOverrides": {"intl": {"version": "WRONG INTL"}},
                    }
                    for difficulty in ("basic", "advanced", "expert", "master", "remaster")
                ],
            }
        ]
    }


def remy(version="Splash", day="2020-11-05"):
    return {
        "chart_key": "1:standard:4",
        "title": "Song",
        "type": "standard",
        "song_id": 1,
        "verified": True,
        "jp_release_version": version,
        "jp_release_date": day,
        "source_url": "https://silentblue.remywiki.com/maimai_DX:Splash",
        "evidence": "Dated Japanese table row",
        "source_revision": "123",
    }


def previous(result, source=None):
    source = deepcopy(source or lxns())
    for song in source["songs"]:
        song["charts"] = []
        for kind, charts in song["difficulties"].items():
            for chart in charts:
                key = (
                    f"{song['id']}:{kind}:ordinary"
                    if chart["difficulty"] < 4
                    else f"{song['id']}:{kind}:4"
                )
                song["charts"].append(
                    {
                        "chart_key": f"{song['id']}:{kind}:{chart['difficulty']}",
                        "type": kind,
                        "difficulty_index": chart["difficulty"],
                        **result["objects"][key],
                    }
                )
    return source


class VersionTests(unittest.TestCase):
    def test_only_jp_sheet_version_ordinary_and_independent_remy(self):
        result = resolve_versions(lxns(), arcade(), [remy()])
        ordinary = result["objects"]["1:standard:ordinary"]
        white = result["objects"]["1:standard:4"]
        self.assertEqual(ordinary["jp_release_version"], "ORANGE")
        self.assertIsNone(ordinary["jp_release_date"])
        self.assertEqual(ordinary["jp_release_provider"], "arcade_songs")
        self.assertEqual(white["jp_release_version"], "Splash")
        self.assertEqual(white["jp_release_provider"], "remywiki")
        self.assertEqual(result["items"][0]["status"], "resolved")

    def test_never_top_level_international_or_remaster_inheritance(self):
        source = arcade(version=None)
        result = resolve_versions(lxns(both=True), source, [])
        self.assertTrue(
            all(row["jp_release_version"] is None for row in result["objects"].values())
        )
        source["songs"][0]["sheets"] = [source["songs"][0]["sheets"][-1]]
        source["songs"][0]["sheets"][0]["version"] = "WRONG WHITE"
        result = resolve_versions(lxns(), source, [])
        self.assertTrue(
            all(row["jp_release_version"] is None for row in result["objects"].values())
        )

    def test_artist_difference_pending_and_identity_select(self):
        raw = arcade(artist="Artist feat. Singer")
        result = resolve_versions(lxns(), raw, [])
        item = result["items"][0]
        self.assertEqual(item["status"], "pending")
        self.assertIn("artist_mismatch", item["reasons"])
        decision = validate_decision(
            result,
            {
                "object_key": item["object_key"],
                "kind": "identity",
                "action": "select",
                "candidate_id": item["candidates"][0]["candidate_id"],
            },
        )
        decided = resolve_versions(
            lxns(), raw, [], adjudications={"revision": 1, "decisions": [decision]}
        )
        self.assertEqual(decided["objects"][item["object_key"]]["jp_release_version"], "ORANGE")
        self.assertEqual(decided["decision_revision"], 1)

    def test_identity_revoke_removes_dependent_automatic_value(self):
        raw = arcade(artist="Artist feat. Singer")
        first = resolve_versions(lxns(), raw, [])
        decision = validate_decision(
            first,
            {
                "object_key": "1:standard:ordinary",
                "kind": "identity",
                "action": "select",
                "candidate_id": first["items"][0]["candidates"][0]["candidate_id"],
            },
        )
        adopted = resolve_versions(lxns(), raw, [], adjudications={"decisions": [decision]})
        self.assertEqual(
            adopted["objects"]["1:standard:ordinary"]["jp_release_provenance"]["identity_decision"],
            decision,
        )
        kept = resolve_versions(lxns(), raw, [], previous(adopted), {"decisions": [decision]})
        self.assertEqual(kept["items"][0]["status"], "resolved")
        manual_decision = validate_decision(
            kept,
            {
                "object_key": "1:standard:ordinary",
                "kind": "version",
                "action": "set",
                "version": "MiLK",
            },
        )
        manual = resolve_versions(
            lxns(), raw, [], previous(kept), {"decisions": [decision, manual_decision]}
        )
        version_revoked = resolve_versions(
            lxns(), raw, [], previous(manual), {"decisions": [decision]}
        )
        self.assertEqual(version_revoked["items"][0]["current"]["version"], "ORANGE")
        self.assertEqual(version_revoked["items"][0]["current"]["provider"], "arcade_songs")
        revoked = resolve_versions(lxns(), raw, [], previous(version_revoked), {"decisions": []})
        self.assertEqual(revoked["items"][0]["status"], "pending")
        self.assertIsNone(revoked["items"][0]["current"]["version"])
        reject = validate_decision(adopted, {**decision, "action": "reject"})
        rejected = resolve_versions(lxns(), raw, [], previous(adopted), {"decisions": [reject]})
        self.assertIsNone(rejected["items"][0]["current"]["version"])

    def test_identity_dependency_source_change_keeps_old_value_but_new_choice_does_not(self):
        raw = arcade(artist="Artist feat. Singer")
        first = resolve_versions(lxns(), raw, [])
        decision = validate_decision(
            first,
            {
                "object_key": "1:standard:ordinary",
                "kind": "identity",
                "action": "select",
                "candidate_id": first["items"][0]["candidates"][0]["candidate_id"],
            },
        )
        adopted = resolve_versions(lxns(), raw, [], adjudications={"decisions": [decision]})
        changed_raw = arcade(artist="Artist feat. Singer", version="PiNK")
        changed = resolve_versions(
            lxns(), changed_raw, [], previous(adopted), {"decisions": [decision]}
        )
        self.assertEqual(changed["items"][0]["status"], "conflict")
        self.assertEqual(changed["items"][0]["current"]["version"], "ORANGE")
        choice = validate_decision(
            changed,
            {
                "object_key": "1:standard:ordinary",
                "kind": "identity",
                "action": "select",
                "candidate_id": changed["items"][0]["candidates"][0]["candidate_id"],
            },
        )
        replaced = resolve_versions(
            lxns(), changed_raw, [], previous(changed), {"decisions": [choice]}
        )
        self.assertEqual(replaced["items"][0]["status"], "resolved")
        self.assertEqual(replaced["items"][0]["current"]["version"], "PiNK")

    def test_reject_adopted_evidence_clears_but_reject_different_new_value_preserves_old(self):
        first = resolve_versions(lxns(), arcade(), [])
        for kind in ("identity", "version"):
            for all_candidates in (False, True):
                with self.subTest(kind=kind, all_candidates=all_candidates):
                    command = {
                        "object_key": "1:standard:ordinary",
                        "kind": kind,
                        "action": "reject",
                    }
                    if not all_candidates:
                        command["candidate_id"] = first["items"][0]["candidates"][0]["candidate_id"]
                    decision = validate_decision(first, command)
                    rejected = resolve_versions(
                        lxns(), arcade(), [], previous(first), {"decisions": [decision]}
                    )
                    self.assertEqual(rejected["items"][0]["status"], "pending")
                    self.assertIsNone(rejected["items"][0]["current"]["version"])
                    changed = resolve_versions(lxns(), arcade(version="PiNK"), [], previous(first))
                    if not all_candidates:
                        command["candidate_id"] = changed["items"][0]["candidates"][0][
                            "candidate_id"
                        ]
                    new_decision = validate_decision(changed, command)
                    kept = resolve_versions(
                        lxns(),
                        arcade(version="PiNK"),
                        [],
                        previous(first),
                        {"decisions": [new_decision]},
                    )
                    self.assertEqual(kept["items"][0]["status"], "conflict")
                    self.assertEqual(kept["items"][0]["current"]["version"], "ORANGE")

    def test_same_title_artist_collision_requires_choice(self):
        raw = arcade()
        raw["songs"] += arcade(external_id="second")["songs"]
        result = resolve_versions(lxns(), raw, [])
        self.assertEqual(result["items"][0]["status"], "conflict")
        self.assertIsNone(result["items"][0]["current"]["version"])

    def test_same_title_distinct_authors_matches_only_right_artist(self):
        raw = arcade(artist="Other")
        raw["songs"] += arcade(version="PiNK", external_id="right")["songs"]
        result = resolve_versions(lxns(), raw, [])
        self.assertEqual(result["items"][0]["current"]["version"], "PiNK")

    def test_old_adopted_source_change_retains_value_and_requires_manual_choice(self):
        first = resolve_versions(lxns(), arcade(), [remy()])
        old = previous(first)
        changed = resolve_versions(
            lxns(), arcade(version="PiNK"), [remy("FESTiVAL", "2022-09-15")], old
        )
        self.assertEqual(changed["items"][0]["status"], "conflict")
        self.assertEqual(changed["items"][0]["current"]["version"], "ORANGE")
        self.assertEqual(changed["items"][1]["current"]["version"], "Splash")
        item = changed["items"][0]
        decision = validate_decision(
            changed,
            {
                "object_key": item["object_key"],
                "kind": "version",
                "action": "select",
                "candidate_id": item["candidates"][0]["candidate_id"],
            },
        )
        final = resolve_versions(
            lxns(), arcade(version="PiNK"), [remy()], old, {"revision": 1, "decisions": [decision]}
        )
        self.assertEqual(final["items"][0]["current"]["version"], "PiNK")
        self.assertEqual(final["items"][0]["current"]["provider"], "manual")

    def test_manual_set_without_candidates_revoke_recomputes(self):
        result = resolve_versions(lxns(), {"songs": []}, [])
        decision = validate_decision(
            result,
            {
                "object_key": "1:standard:ordinary",
                "kind": "version",
                "action": "set",
                "version": "Future JP",
            },
        )
        manual = resolve_versions(
            lxns(), {"songs": []}, [], adjudications={"revision": 1, "decisions": [decision]}
        )
        self.assertEqual(manual["items"][0]["current"]["version"], "Future JP")
        revoked = resolve_versions(
            lxns(), arcade(), [], previous(manual), {"revision": 2, "decisions": []}
        )
        self.assertEqual(revoked["items"][0]["current"]["version"], "ORANGE")
        self.assertEqual(revoked["items"][0]["current"]["provider"], "arcade_songs")
        empty = resolve_versions(
            lxns(), {"songs": []}, [], previous(manual), {"revision": 2, "decisions": []}
        )
        self.assertIsNone(empty["items"][0]["current"]["version"])

    def test_identity_fingerprint_excludes_constants_but_binds_original_strings(self):
        source = lxns()
        fp = object_fingerprint(source["songs"][0], "standard")
        source["songs"][0]["difficulties"]["standard"][0]["level_value"] = 15
        self.assertEqual(fp, object_fingerprint(source["songs"][0], "standard"))
        source["songs"][0]["title"] = " Song "
        self.assertNotEqual(fp, object_fingerprint(source["songs"][0], "standard"))
        initial = resolve_versions(lxns(), arcade(), [])
        decision = validate_decision(
            initial,
            {
                "object_key": "1:standard:ordinary",
                "kind": "version",
                "action": "set",
                "version": "PiNK",
            },
        )
        changed = resolve_versions(
            source, arcade(), [], previous(initial), {"revision": 1, "decisions": [decision]}
        )
        self.assertEqual(changed["items"][0]["status"], "conflict")
        self.assertIn("decision_stale", changed["items"][0]["reasons"])

    def test_evidence_bound_decision_invalidates_after_source_changes(self):
        raw = arcade(artist="Artist feat. Singer")
        first = resolve_versions(lxns(), raw, [])
        candidate = first["items"][0]["candidates"][0]
        dec = validate_decision(
            first,
            {
                "object_key": "1:standard:ordinary",
                "kind": "identity",
                "action": "select",
                "candidate_id": candidate["candidate_id"],
            },
        )
        raw["songs"][0]["artist"] = "Artist feat. New Singer"
        changed = resolve_versions(
            lxns(), raw, [], adjudications={"revision": 1, "decisions": [dec]}
        )
        self.assertIn("decision_stale", changed["items"][0]["reasons"])
        with self.assertRaises(ValueError):
            validate_decision(changed, dec)

    def test_keep_is_stable_across_rebuild(self):
        first = resolve_versions(lxns(), arcade(), [])
        old = previous(first)
        changed = resolve_versions(lxns(), arcade(version="PiNK"), [], old)
        dec = validate_decision(
            changed, {"object_key": "1:standard:ordinary", "kind": "version", "action": "keep"}
        )
        manual = resolve_versions(
            lxns(), arcade(version="PiNK"), [], old, {"revision": 1, "decisions": [dec]}
        )
        rebuilt = resolve_versions(
            lxns(),
            arcade(version="PiNK"),
            [],
            previous(manual),
            {"revision": 1, "decisions": [dec]},
        )
        self.assertEqual(rebuilt["items"][0]["status"], "manual")
        self.assertEqual(rebuilt["items"][0]["current"]["version"], "ORANGE")

    def test_remy_conflicts_survive_as_review_and_never_autoselect(self):
        first = resolve_versions(lxns(), arcade(), [remy()])
        conflicts = {
            "conflicts": [
                {
                    "chart_key": "1:standard:4",
                    "reason": "conflicting_debut_evidence",
                    "candidates": [remy("FESTiVAL", "2022-09-15")],
                }
            ]
        }
        result = resolve_versions(
            lxns(), arcade(), [remy()], previous(first), remy_conflicts=conflicts
        )
        self.assertEqual(result["items"][1]["status"], "conflict")
        self.assertEqual(result["items"][1]["current"]["version"], "Splash")
        self.assertEqual(len(result["items"][1]["candidates"]), 2)

    def test_revoked_manual_value_is_not_restored_by_remy_conflict(self):
        first = resolve_versions(lxns(), arcade(), [remy()])
        decision = validate_decision(
            first,
            {"object_key": "1:standard:4", "kind": "version", "action": "set", "version": "MiLK"},
        )
        manual = resolve_versions(
            lxns(), arcade(), [remy()], adjudications={"decisions": [decision]}
        )
        conflicts = {
            "conflicts": [
                {
                    "chart_key": "1:standard:4",
                    "reason": "conflicting_debut_evidence",
                    "candidates": [remy("FESTiVAL", "2022-09-15")],
                }
            ]
        }
        revoked = resolve_versions(
            lxns(), arcade(), [remy()], previous(manual), {"decisions": []}, conflicts
        )
        self.assertEqual(revoked["items"][1]["status"], "conflict")
        self.assertIsNone(revoked["items"][1]["current"]["version"])

    def test_unknown_source_version_pending_manual_value_allowed(self):
        result = resolve_versions(lxns(), arcade(version="UNKNOWN NEXT"), [])
        self.assertIsNone(result["items"][0]["current"]["version"])
        self.assertIn("unknown_jp_version", result["items"][0]["reasons"])
        decision = validate_decision(
            result,
            {
                "object_key": "1:standard:ordinary",
                "kind": "version",
                "action": "set",
                "version": "UNKNOWN NEXT",
            },
        )
        final = resolve_versions(
            lxns(), arcade(version="UNKNOWN NEXT"), [], adjudications={"decisions": [decision]}
        )
        self.assertEqual(final["items"][0]["current"]["version"], "UNKNOWN NEXT")

    def test_remy_evidence_file_location_does_not_change_fingerprint(self):
        record = dict(remy(), evidence_file="/old/absolute/cache.html", evidence_sha256="abc")
        first = resolve_versions(lxns(), arcade(), [record])
        record["evidence_file"] = "remy-evidence/abc.html"
        second = resolve_versions(lxns(), arcade(), [record])
        self.assertEqual(first["items"][1]["candidates"], second["items"][1]["candidates"])

    def test_manual_source_change_keeps_value_conflict_then_keep_acknowledges(self):
        first = resolve_versions(lxns(), arcade(), [])
        dec = validate_decision(
            first,
            {
                "object_key": "1:standard:ordinary",
                "kind": "version",
                "action": "set",
                "version": "MiLK",
            },
        )
        manual = resolve_versions(
            lxns(), arcade(), [], adjudications={"revision": 1, "decisions": [dec]}
        )
        source_changed = arcade(version="PiNK")
        conflict = resolve_versions(
            lxns(), source_changed, [], previous(manual), {"revision": 1, "decisions": [dec]}
        )
        self.assertEqual(conflict["items"][0]["status"], "conflict")
        self.assertEqual(conflict["items"][0]["current"]["version"], "MiLK")
        self.assertEqual(conflict["items"][0]["current"]["provider"], "manual")
        self.assertIn("decision_stale", conflict["items"][0]["reasons"])
        keep = validate_decision(
            conflict, {"object_key": "1:standard:ordinary", "kind": "version", "action": "keep"}
        )
        accepted = resolve_versions(
            lxns(), source_changed, [], previous(conflict), {"revision": 2, "decisions": [keep]}
        )
        self.assertEqual(accepted["items"][0]["status"], "manual")
        self.assertEqual(accepted["items"][0]["current"]["version"], "MiLK")
        rollback = resolve_versions(
            lxns(), arcade(), [], previous(accepted), {"revision": 2, "decisions": [keep]}
        )
        self.assertEqual(rollback["items"][0]["status"], "conflict")
        self.assertEqual(rollback["items"][0]["current"]["version"], "MiLK")

    def test_manual_set_with_empty_candidates_reopens_when_source_arrives(self):
        first = resolve_versions(lxns(), {"songs": []}, [])
        dec = validate_decision(
            first,
            {
                "object_key": "1:standard:ordinary",
                "kind": "version",
                "action": "set",
                "version": "PiNK",
            },
        )
        manual = resolve_versions(lxns(), {"songs": []}, [], adjudications={"decisions": [dec]})
        changed = resolve_versions(lxns(), arcade(), [], previous(manual), {"decisions": [dec]})
        self.assertEqual(changed["items"][0]["status"], "conflict")
        self.assertEqual(changed["items"][0]["current"]["version"], "PiNK")

    def test_arcade_row_order_does_not_invalidate_evidence(self):
        source = arcade()
        first = resolve_versions(lxns(), source, [])
        source["songs"][0]["sheets"].reverse()
        second = resolve_versions(lxns(), source, [])
        self.assertEqual(first["items"][0]["candidates"], second["items"][0]["candidates"])

    def test_reject_suppresses_automatic_candidate(self):
        first = resolve_versions(lxns(), arcade(), [])
        dec = validate_decision(
            first, {"object_key": "1:standard:ordinary", "kind": "identity", "action": "reject"}
        )
        rejected = resolve_versions(lxns(), arcade(), [], adjudications={"decisions": [dec]})
        self.assertIsNone(rejected["items"][0]["current"]["version"])


if __name__ == "__main__":
    unittest.main()
