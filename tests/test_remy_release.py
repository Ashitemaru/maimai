import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

from scripts.remy_release import (
    FetchError,
    NoRedirect,
    RemyFetcher,
    cached_pages,
    read_json,
    resolve,
    run,
    version_pages,
    write_json,
)
from tests.fixtures.remy import write_remy_catalog

SOURCE = "https://silentblue.remywiki.com/maimai_DX:FESTiVAL"


def catalog(both=False):
    return {
        "songs": [
            {
                "id": 1,
                "title": "Song",
                "artist": "Artist",
                "difficulties": {
                    "standard": [{"difficulty": 4}],
                    "dx": [{"difficulty": 4}] if both else [],
                },
            }
        ]
    }


def page(kind="standard", date="2022-09-15", revision="123"):
    return {
        "source_url": SOURCE,
        "source_revision": revision,
        "issues": [],
        "rows": [
            {
                "title": "Song",
                "artist": "Artist",
                "chart_type": kind,
                "date": date,
                "version": "FESTiVAL",
                "evidence_row": ["Song", "Artist", "13"],
            }
        ],
    }


class Response(io.BytesIO):
    def __init__(self, body, url=SOURCE):
        super().__init__(body)
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.url = url

    def geturl(self):
        return self.url


EMPTY_VALID = b"""<title>maimai DX:FESTiVAL - RemyWiki</title>
<script>{"wgPageName":"maimai_DX:FESTiVAL","wgRevisionId":123}</script>
<h2>New Songs / DX Charts</h2><table>
<tr><th>Song</th><th>Artist</th><th>BASIC</th><th>ADVANCED</th>
<th>EXPERT</th><th>MASTER</th><th>Re:MASTER</th></tr>
<tr><th id="20220915" colspan="7">Launch</th></tr>
<tr><td>Song</td><td>Artist</td><td>2</td><td>5</td><td>9</td><td>12</td><td>-</td></tr></table>"""


class MatchingTests(unittest.TestCase):
    def test_cached_replay_preserves_known_evidence_and_requires_artist_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document, old, expected_records = write_remy_catalog(root)
            pages, errors = cached_pages(root, root / "cache")
            report = resolve(document, old, pages)
            self.assertEqual(errors, [])
            self.assertEqual(report["already_verified"], 1)
            self.assertEqual(report["resolved_count"], 2)
            self.assertEqual(report["status"], "partial")
            self.assertEqual(
                [record["chart_key"] for record in report["conflicts"]], ["4:standard:4"]
            )
            self.assertEqual(report["conflicts"][0]["reason"], "identity_mismatch")
            expected = {record["chart_key"]: record for record in expected_records}
            self.assertEqual(
                {record["chart_key"] for record in report["additions"]}, {"2:standard:4", "3:dx:4"}
            )
            for actual in report["additions"]:
                with self.subTest(chart=actual["chart_key"]):
                    reference = expected[actual["chart_key"]]
                    for field in (
                        "jp_release_version",
                        "jp_release_date",
                        "source_url",
                        "source_revision",
                        "evidence_sha256",
                        "evidence_row",
                    ):
                        self.assertEqual(actual[field], reference[field])
                    self.assertEqual(
                        Path(actual["evidence_file"]), (root / reference["evidence_file"]).resolve()
                    )

    def test_all_verified_noop_never_fetches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, old, records = write_remy_catalog(root)
            write_json(root / "remy_release_overrides.json", {"overrides": old + records})
            fetcher = Mock(requests=0)
            result = run(root, fetcher=fetcher)
            self.assertEqual(result["already_verified"], 4)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["page_count"], 0)
            fetcher.fetch.assert_not_called()

    def test_unspecified_format_is_ambiguous_even_if_one_sibling_already_known(self):
        known = resolve(catalog(True), [], [page("standard")])["additions"]
        result = resolve(catalog(True), known, [page(None)])
        self.assertEqual(result["resolved_count"], 0)
        self.assertEqual(result["pending"][0]["chart_key"], "1:dx:4")
        self.assertEqual(result["pending"][0]["reason"], "conflicting_evidence")
        self.assertEqual(len(result["conflicts"]), 2)

    def test_conflict_does_not_replace_known_or_accept_new(self):
        known = resolve(catalog(), [], [page()])["additions"]
        for existing in (known, []):
            result = resolve(catalog(), existing, [page(), page(date="2022-10-01")])
            self.assertEqual(result["resolved_count"], 0)
            self.assertEqual(len(result["conflicts"]), 1)
            self.assertEqual(result["status"], "partial")

    def test_new_revision_missing_is_pending(self):
        result = resolve(catalog(), [], [page(revision=None)])
        self.assertEqual(result["resolved_count"], 0)
        self.assertEqual(len(result["pending"]), 1)

    def test_existing_non_remy_authority_not_trusted(self):
        known = resolve(catalog(), [], [page()])["additions"]
        known[0]["source_url"] = "https://example.com/song"
        self.assertEqual(resolve(catalog(), known, [])["already_verified"], 0)

    def test_artist_mismatch_does_not_accept_unique_title_or_discard_prior_evidence(self):
        known = resolve(catalog(), [], [page()])["additions"]
        changed = page()
        changed["rows"][0]["artist"] = "Different artist"
        for prior in ([], known):
            with self.subTest(known=bool(prior)):
                result = resolve(catalog(), prior, [changed])
                self.assertEqual(result["additions"], [])
                self.assertEqual(result["already_verified"], len(prior))
                self.assertEqual(result["conflicts"][0]["reason"], "identity_mismatch")
                self.assertFalse(result["conflicts"][0]["candidates"][0]["verified"])
                self.assertEqual(result["conflicts"][0]["current"], prior[0] if prior else None)

    def test_same_title_type_with_different_authors_requires_exact_unique_artist(self):
        data = catalog()
        duplicate = json.loads(json.dumps(data["songs"][0]))
        duplicate.update(id=2, artist="Other artist")
        data["songs"].append(duplicate)
        result = resolve(data, [], [page()])
        self.assertEqual([r["chart_key"] for r in result["additions"]], ["1:standard:4"])
        self.assertEqual([r["chart_key"] for r in result["pending"]], ["2:standard:4"])

    def test_recheck_fetches_known_chart_and_keeps_original_on_version_or_date_conflict(self):
        known = resolve(catalog(), [], [page()])["additions"]
        for field, changed in [("jp_release_version", "Splash"), ("jp_release_date", "2022-10-01")]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_json(root / "raw/lxns_songs.json", catalog())
                write_json(root / "remy_release_overrides.json", {"overrides": known})
                fetched = page()
                fetched["rows"][0]["version" if field.endswith("version") else "date"] = changed
                fetcher = Mock(requests=1)
                fetcher.fetch.return_value = fetched
                result = run(root, fetcher=fetcher, page_urls=[SOURCE], recheck=True)
                fetcher.fetch.assert_called_once_with(SOURCE)
                self.assertEqual(result["proposed_overrides"]["overrides"], known)
                self.assertEqual(result["conflicts"][0]["current"], known[0])
                self.assertEqual(result["checked_chart_keys"], ["1:standard:4"])
                self.assertTrue(result["recheck_complete"])
                self.assertEqual(
                    read_json(root / "remy_release_overrides.json")["overrides"], known
                )

    def test_manual_final_provider_skips_missing_fetch_but_recheck_reports_disagreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "raw/lxns_songs.json", catalog())
            write_json(root / "remy_release_overrides.json", {"overrides": []})
            manual = {
                "chart_key": "1:standard:4",
                "jp_release_provider": "manual",
                "jp_release_version": "Splash",
                "jp_release_date": None,
                "release_status": "manual",
            }
            write_json(root / "catalog.json", {"songs": [{"charts": [manual]}]})
            fetcher = Mock(requests=0)
            result = run(root, fetcher=fetcher)
            fetcher.fetch.assert_not_called()
            self.assertEqual(result["manual_protected"], 1)
            self.assertEqual(result["pending"], [])
            fetcher.fetch.return_value = page()
            result = run(root, fetcher=fetcher, page_urls=[SOURCE], recheck=True)
            self.assertEqual(result["additions"], [])
            self.assertEqual(result["proposed_overrides"]["overrides"], [])
            self.assertEqual(result["conflicts"][0]["current"], manual)

    def test_manual_same_version_without_date_is_not_overwritten_or_reported_as_conflict(self):
        manual = {
            "chart_key": "1:standard:4",
            "jp_release_provider": "manual",
            "jp_release_version": "FESTiVAL",
            "jp_release_date": None,
        }
        result = resolve(catalog(), [], [page()], protected_records=[manual])
        self.assertEqual(result["additions"], [])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["pending"], [])

    def test_tampered_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.html").write_bytes(EMPTY_VALID)
            write_json(
                root / "index.json",
                {"pages": [{"source_url": SOURCE, "file": "bad.html", "sha256": "wrong"}]},
            )
            pages, errors = cached_pages(root, root)
            self.assertEqual(pages, [])
            self.assertIn("SHA256", errors[0]["reason"])


class FetchTests(unittest.TestCase):
    def test_corrupt_compressed_response_becomes_fetch_error(self):
        for encoding, body in (("gzip", b"\x1f\x8b"), ("deflate", b"broken"), ("zstd", b"broken")):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as tmp:
                response = Response(body)
                response.headers["Content-Encoding"] = encoding
                fetcher = RemyFetcher(Path(tmp), opener=Mock(return_value=response))
                with self.assertRaises(FetchError):
                    fetcher.fetch(SOURCE)
                self.assertFalse((Path(tmp) / "index.json").exists())

    def test_403_stops_and_persists_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = Mock(side_effect=HTTPError(SOURCE, 403, "blocked", {}, None))
            fetcher = RemyFetcher(Path(tmp), opener=opener, now=lambda: 100000)
            with self.assertRaisesRegex(FetchError, "http_403"):
                fetcher.fetch(SOURCE)
            restarted = RemyFetcher(Path(tmp), opener=opener, now=lambda: 100001)
            with self.assertRaisesRegex(FetchError, "remy_cooldown"):
                restarted.fetch(SOURCE)
            self.assertEqual(opener.call_count, 1)

    def test_429_respects_retry_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = Mock(
                side_effect=HTTPError(SOURCE, 429, "slow down", {"Retry-After": "7200"}, None)
            )
            fetcher = RemyFetcher(Path(tmp), opener=opener, now=lambda: 100000)
            with self.assertRaisesRegex(FetchError, "http_429"):
                fetcher.fetch(SOURCE)
            self.assertEqual(fetcher.state["retry_after"], 107200)

    def test_challenge_200_stops_without_caching(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = Mock(return_value=Response(b"<title>Just a moment...</title>"))
            fetcher = RemyFetcher(Path(tmp), opener=opener, now=lambda: 100000)
            with self.assertRaisesRegex(FetchError, "access_challenge_page"):
                fetcher.fetch(SOURCE)
            self.assertTrue(fetcher.stopped)
            self.assertFalse((Path(tmp) / "index.json").exists())

    def test_empty_valid_page_cached_and_request_spacing_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = [100000]

            def sleep(seconds):
                self.assertEqual(seconds, 60)
                current[0] += seconds

            opener = Mock(side_effect=lambda *a, **kw: Response(EMPTY_VALID))
            fetcher = RemyFetcher(
                Path(tmp),
                opener=opener,
                max_requests=2,
                interval=0,
                now=lambda: current[0],
                sleep=sleep,
            )
            self.assertEqual(fetcher.fetch(SOURCE)["rows"], [])
            self.assertTrue((Path(tmp) / "index.json").exists())
            fetcher.fetch(SOURCE)
            self.assertEqual(current[0], 100060)
            with self.assertRaisesRegex(FetchError, "request_budget_exhausted"):
                fetcher.fetch(SOURCE)
            self.assertEqual(opener.call_count, 2)

    def test_default_scan_progress_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages = version_pages()
            write_json(
                root / "fetch_state.json", {"attempted_pages": {url: 100000 for url in pages[:8]}}
            )
            fetcher = RemyFetcher(root)
            self.assertEqual(fetcher.ordered_pages()[:8], pages[8:16])

    def test_redirect_does_not_issue_followup_request(self):
        self.assertIsNone(
            NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example")
        )

    def test_failure_is_pending_and_proposal_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "raw/lxns_songs.json", catalog())
            write_json(root / "remy_release_overrides.json", {"overrides": []})
            opener = Mock(side_effect=HTTPError(SOURCE, 403, "blocked", {}, None))
            fetcher = RemyFetcher(root / "cache", opener=opener)
            result = run(root, fetcher=fetcher, page_urls=[SOURCE, SOURCE])
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["fetch_count"], 1)
            self.assertEqual(result["proposed_overrides"]["overrides"], [])
            self.assertEqual(result["fetch_errors"][0]["reason"], "http_403")


if __name__ == "__main__":
    unittest.main()
