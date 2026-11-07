import unittest

from scripts.catalog_versions import object_fingerprint, resolve_versions


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

    def test_artist_difference_stays_pending(self):
        result = resolve_versions(lxns(), arcade(artist="Artist feat. Singer"), [])
        item = result["items"][0]
        self.assertEqual(item["status"], "pending")
        self.assertIn("artist_mismatch", item["reasons"])
        self.assertIsNone(result["objects"][item["object_key"]]["jp_release_version"])

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

    def test_identity_fingerprint_excludes_constants_but_binds_original_strings(self):
        source = lxns()
        fp = object_fingerprint(source["songs"][0], "standard")
        source["songs"][0]["difficulties"]["standard"][0]["level_value"] = 15
        self.assertEqual(fp, object_fingerprint(source["songs"][0], "standard"))
        source["songs"][0]["title"] = " Song "
        self.assertNotEqual(fp, object_fingerprint(source["songs"][0], "standard"))

    def test_remy_conflicts_survive_as_review_and_never_autoselect(self):
        conflicts = {
            "conflicts": [
                {
                    "chart_key": "1:standard:4",
                    "reason": "conflicting_debut_evidence",
                    "candidates": [remy("FESTiVAL", "2022-09-15")],
                }
            ]
        }
        result = resolve_versions(lxns(), arcade(), [remy()], remy_conflicts=conflicts)
        self.assertEqual(result["items"][1]["status"], "conflict")
        self.assertIsNone(result["items"][1]["current"]["version"])
        self.assertEqual(len(result["items"][1]["candidates"]), 2)

    def test_unknown_source_version_stays_pending(self):
        result = resolve_versions(lxns(), arcade(version="UNKNOWN NEXT"), [])
        self.assertIsNone(result["items"][0]["current"]["version"])
        self.assertIn("unknown_jp_version", result["items"][0]["reasons"])

    def test_remy_evidence_file_location_does_not_change_fingerprint(self):
        record = dict(remy(), evidence_file="/old/absolute/cache.html", evidence_sha256="abc")
        first = resolve_versions(lxns(), arcade(), [record])
        record["evidence_file"] = "remy-evidence/abc.html"
        second = resolve_versions(lxns(), arcade(), [record])
        self.assertEqual(first["items"][1]["candidates"], second["items"][1]["candidates"])

    def test_arcade_row_order_does_not_invalidate_evidence(self):
        source = arcade()
        first = resolve_versions(lxns(), source, [])
        source["songs"][0]["sheets"].reverse()
        second = resolve_versions(lxns(), source, [])
        self.assertEqual(first["items"][0]["candidates"], second["items"][0]["candidates"])


if __name__ == "__main__":
    unittest.main()
