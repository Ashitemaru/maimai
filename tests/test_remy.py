"""RemyWiki JP release evidence: cached pages are parsed, matched, and never trusted blindly."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

from scripts.remy_release import FetchError, RemyFetcher, cached_pages, resolve
from tests.fixtures.remy import write_remy_catalog

SOURCE = "https://silentblue.remywiki.com/maimai_DX:FESTiVAL"


def page(date="2022-09-15"):
    row = {"title": "Song", "artist": "Artist", "chart_type": "standard", "date": date}
    row |= {"version": "FESTiVAL", "evidence_row": ["Song", "Artist", "13"]}
    return {"source_url": SOURCE, "source_revision": "123", "issues": [], "rows": [row]}


CATALOG = {
    "songs": [
        {
            "id": 1,
            "title": "Song",
            "artist": "Artist",
            "difficulties": {"standard": [{"difficulty": 4}], "dx": []},
        }
    ]
}


class RemyTests(unittest.TestCase):
    def test_cached_pages_yield_dated_evidence_and_artist_mismatch_is_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document, old, expected = write_remy_catalog(root)
            pages, errors = cached_pages(root, root / "cache")
            report = resolve(document, old, pages)
        self.assertEqual(errors, [])
        self.assertEqual(report["already_verified"], 1)
        self.assertEqual([r["chart_key"] for r in report["conflicts"]], ["4:standard:4"])
        expected = {r["chart_key"]: r for r in expected}
        self.assertEqual({r["chart_key"] for r in report["additions"]}, {"2:standard:4", "3:dx:4"})
        for actual in report["additions"]:
            reference = expected[actual["chart_key"]]
            for field in (
                "jp_release_version",
                "jp_release_date",
                "evidence_sha256",
                "evidence_row",
            ):
                self.assertEqual(actual[field], reference[field], (actual["chart_key"], field))

    def test_conflicting_dates_neither_replace_known_nor_accept_new(self):
        known = resolve(CATALOG, [], [page()])["additions"]
        for existing in (known, []):
            result = resolve(CATALOG, existing, [page(), page(date="2022-10-01")])
            self.assertEqual((result["resolved_count"], len(result["conflicts"])), (0, 1))

    def test_403_stops_fetching_and_cooldown_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = Mock(side_effect=HTTPError(SOURCE, 403, "blocked", {}, None))
            with self.assertRaisesRegex(FetchError, "http_403"):
                RemyFetcher(Path(tmp), opener=opener, now=lambda: 100000).fetch(SOURCE)
            with self.assertRaisesRegex(FetchError, "remy_cooldown"):
                RemyFetcher(Path(tmp), opener=opener, now=lambda: 100001).fetch(SOURCE)
            self.assertEqual(opener.call_count, 1)
