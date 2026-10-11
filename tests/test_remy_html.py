"""Offline parser regression cases with synthetic, reproducible evidence."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.remy_html import parse_version_page
from tests.fixtures.remy import write_remy_catalog

URL = "https://silentblue.remywiki.com/maimai_DX:FESTiVAL"
HEADER = "<tr><th>Song</th><th>Artist</th><th>Basic</th><th>Advanced</th><th>Expert</th><th>Master</th><th>Re:Master</th></tr>"
DATE = '<tr><th colspan="99" id="20220915">Launch</th></tr>'


def row(title="日本語の曲", level="13+", *, note=""):
    return f'<tr><td><a href="/Romanized_title" title="Romanized title">{title}</a>{note}</td><td>作曲者</td><td>-</td><td>-</td><td>-</td><td>-</td><td>{level}</td></tr>'


def page(body, *, identity="maimai_DX:FESTiVAL", heading="New Songs / DX Charts"):
    return f"""<!DOCTYPE html><html><head><title>maimai でらっくす FESTiVAL - SilentBlue.RemyWiki</title>
<script>RLCONF={{"wgPageName":{json.dumps(identity)},"wgCurRevisionId":999,"wgRevisionId":123}};</script></head>
<body><h1>maimai でらっくす FESTiVAL</h1><h2>{heading}</h2>{body}</body></html>"""


class RemyHtmlTests(unittest.TestCase):
    def test_cached_version_pages_recover_synthetic_evidence_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, expected = write_remy_catalog(root)
            files = {record["evidence_file"] for record in expected}
            self.assertEqual(len(files), 2)
            count = 0
            for file in files:
                source_records = [record for record in expected if record["evidence_file"] == file]
                source = source_records[0]
                parsed = parse_version_page(
                    (root / file).read_text(),
                    source["source_url"],
                    version=source["jp_release_version"],
                )
                with self.subTest(file=file):
                    self.assertEqual(parsed["issues"], [])
                    self.assertEqual(parsed["source_revision"], source["source_revision"])
                    self.assertEqual(len(parsed["rows"]), len(source_records))
                    for record in source_records:
                        hits = [row for row in parsed["rows"] if row["title"] == record["title"]]
                        self.assertEqual(len(hits), 1, record["title"])
                        hit = hits[0]
                        self.assertEqual(hit["date"], record["jp_release_date"])
                        self.assertEqual(hit["version"], record["jp_release_version"])
                        self.assertEqual(hit["chart_type"], record["type"])
                        self.assertEqual(hit["evidence_row"], record["evidence_row"])
                        count += 1
            self.assertEqual(count, 3)

    def test_visible_japanese_title_and_revision_not_link_title(self):
        result = parse_version_page(
            page("<table>" + HEADER + DATE + row(note="<br><small>DX Charts</small>") + "</table>"),
            URL,
        )
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["source_revision"], "123")
        self.assertEqual(result["rows"][0]["title"], "日本語の曲")
        self.assertEqual(result["rows"][0]["chart_type"], "dx")
        self.assertEqual(result["rows"][0]["date"], "2022-09-15")

    def test_dx_version_does_not_imply_dx_chart_format(self):
        result = parse_version_page(
            page("<table>" + HEADER + DATE + row("Standard") + "</table>"), URL
        )
        self.assertIsNone(result["rows"][0]["chart_type"])

    def test_old_easy_column_and_two_level_header(self):
        header = '<tr><th rowspan="2">Song</th><th rowspan="2">Artist</th><th colspan="6">Level</th></tr><tr><th>Easy</th><th>Basic</th><th>Advanced</th><th>Expert</th><th>Master</th><th>Re:Master</th></tr>'
        html = page(
            '<table><tr><th colspan="99" id="20160630">Launch</th></tr>'
            + header
            + "<tr><td>曲名</td><td>Artist</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>11+</td></tr></table>",
            identity="maimai:PiNK_PLUS",
        ).replace("maimai でらっくす FESTiVAL", "maimai PiNK PLUS")
        result = parse_version_page(html, "https://silentblue.remywiki.com/maimai:PiNK_PLUS")
        self.assertEqual(result["issues"], [])
        self.assertEqual(
            [(r["title"], r["chart_type"], r["date"], r["remaster_level"]) for r in result["rows"]],
            [("曲名", "standard", "2016-06-30", "11+")],
        )

    def test_rowspans_colspans_formats_and_repeated_headers(self):
        header = '<tr><th colspan="2" rowspan="2">Song</th><th rowspan="2">Artist</th><th colspan="5">Level</th></tr><tr><th>Basic</th><th>Advanced</th><th>Expert</th><th>Master</th><th>Re:Master</th></tr>'
        body = "<table>" + header + DATE
        body += '<tr><td rowspan="2">Shared song</td><td>STD</td><td rowspan="2">Artist</td><td>-</td><td>-</td><td>-</td><td>-</td><td>13</td></tr>'
        body += "<tr><td>DX</td><td>-</td><td>-</td><td>-</td><td>-</td><td>14</td></tr>"
        body += (
            header
            + '<tr><td colspan="2">Next song</td><td>Artist</td><td>-</td><td>-</td><td>-</td><td>-</td><td>12</td></tr></table>'
        )
        result = parse_version_page(page(body), URL)
        self.assertEqual(result["issues"], [])
        self.assertEqual(
            [(r["title"], r["chart_type"], r["remaster_level"]) for r in result["rows"]],
            [
                ("Shared song", "standard", "13"),
                ("Shared song", "dx", "14"),
                ("Next song", None, "12"),
            ],
        )
        self.assertEqual(result["rows"][1]["evidence_row"][:3], ["Shared song", "DX", "Artist"])

    def test_rejects_changes_removals_full_song_lists_and_regions(self):
        table = "<table>" + HEADER + DATE + row() + "</table>"
        for section in [
            "Difficulty Changes",
            "Removed Songs",
            "Revived Songs",
            "All Songs",
            "New Songs (China)",
        ]:
            with self.subTest(section=section):
                result = parse_version_page(page(table, heading=section), URL)
                self.assertEqual(result["rows"], [])
                self.assertIn("missing_new_songs_section", result["issues"])
        for excluded in [
            "<h3>International version</h3>" + table,
            table.replace("<table>", "<table><caption>China releases</caption>"),
        ]:
            result = parse_version_page(page(excluded), URL)
            self.assertEqual(result["rows"], [])
        result = parse_version_page(
            page(table + "<h2>Difficulty Changes</h2>" + table.replace("日本語の曲", "Old song")),
            URL,
        )
        self.assertEqual([r["title"] for r in result["rows"]], ["日本語の曲"])

    def test_regional_and_utage_blocks_cannot_emit_debuts(self):
        body = "<table>" + HEADER + DATE
        body += '<tr><th colspan="99">宴会場</th></tr>' + row("UTAGE song")
        body += '<tr><th colspan="99">POPS &amp; ANIME</th></tr>' + row("Japanese song")
        body += '<tr><th colspan="99">China release</th></tr>' + row("Chinese song")
        body += '<tr><th colspan="99">2022-09-16</th></tr>' + row("Still Chinese")
        body += '<tr><th colspan="99">Japan</th></tr>' + row("Back to Japan") + "</table>"
        result = parse_version_page(page(body), URL)
        self.assertEqual([r["title"] for r in result["rows"]], ["Japanese song", "Back to Japan"])

    def test_dates_in_child_headings_and_inner_anchor(self):
        body = (
            '<h3><span id="20220920">September 20th, 2022</span></h3><table>'
            + HEADER
            + row("First")
        )
        body += (
            '<tr><th colspan="99"><span id="20220921">Update</span></th></tr>'
            + row("Second")
            + "</table>"
        )
        result = parse_version_page(page(body), URL)
        self.assertEqual(result["issues"], [])
        self.assertEqual([r["date"] for r in result["rows"]], ["2022-09-20", "2022-09-21"])

    def test_no_date_and_invalid_date_fail_closed(self):
        result = parse_version_page(page("<table>" + HEADER + row() + "</table>"), URL)
        self.assertEqual(result["rows"], [])
        self.assertTrue(any(i.startswith("missing_title_or_date") for i in result["issues"]))
        result = parse_version_page(
            page(
                "<table>"
                + HEADER
                + DATE
                + '<tr><th colspan="99" id="20221340">Launch</th></tr>'
                + row()
                + "</table>"
            ),
            URL,
        )
        self.assertEqual(result["rows"], [])
        self.assertIn("invalid_date_heading", result["issues"])

    def test_empty_re_master_column_is_success_but_layout_failure_is_not(self):
        result = parse_version_page(
            page("<table>" + HEADER + DATE + row(level="-") + "</table>"), URL
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["issues"], [])
        result = parse_version_page(
            page(
                "<table>"
                + HEADER.replace("Re:Master", "Unknown new column")
                + DATE
                + row()
                + "</table>"
            ),
            URL,
        )
        self.assertEqual(result["rows"], [])
        self.assertIn("missing_new_songs_table_header", result["issues"])

    def test_wrong_region_url_metadata_and_requested_version(self):
        html = page("<table>" + HEADER + DATE + row() + "</table>")
        for source, options in [
            (URL + "_(China)", {}),
            (URL, {"version": "Splash"}),
            (URL.replace("FESTiVAL", "Splash"), {}),
        ]:
            with self.subTest(source=source, options=options):
                result = parse_version_page(html, source, **options)
                self.assertEqual(result["rows"], [])
                self.assertTrue(result["issues"])
        self.assertEqual(parse_version_page(html, URL, version="FESTiVAL")["issues"], [])

    def test_archive_source_and_dx_aliases(self):
        base = "<table>" + HEADER + DATE + row() + "</table>"
        for name, label, expected in [
            ("1st", "maimai でらっくす", "DX"),
            ("PLUS", "maimai でらっくす PLUS", "DX PLUS"),
        ]:
            html = page(base, identity="maimai_DX:" + name).replace(
                "maimai でらっくす FESTiVAL", label
            )
            source = (
                "https://web.archive.org/web/20260929001815id_/https://silentblue.remywiki.com/maimai_DX:"
                + name
            )
            result = parse_version_page(html, source)
            self.assertEqual(result["issues"], [])
            self.assertEqual(result["version"], expected)

    def test_challenge_empty_non_article_and_wrong_origin(self):
        samples = [
            ("<html><title>Just a moment...</title></html>", URL),
            ("", URL),
            ("<html><title>Proxy error</title></html>", URL),
            (page(""), "https://example.com/maimai_DX:FESTiVAL"),
        ]
        for html, url in samples:
            with self.subTest(html=html, url=url):
                result = parse_version_page(html, url)
                self.assertEqual(result["rows"], [])
                self.assertTrue(result["issues"])

    def test_reworked_rows_and_sections_never_count_as_debuts(self):
        table = "<table>" + HEADER + DATE + row() + "</table>"
        for heading in ("Readjusted Charts", "Reworked charts", "Chart replacements"):
            with self.subTest(heading=heading):
                parsed = parse_version_page(page("<h3>" + heading + "</h3>" + table), URL)
                self.assertEqual(parsed["rows"], [])
                inner = (
                    "<table>"
                    + HEADER
                    + DATE
                    + '<tr><th colspan="99">'
                    + heading
                    + "</th></tr>"
                    + row()
                    + "</table>"
                )
                self.assertEqual(parse_version_page(page(inner), URL)["rows"], [])
        for annotation in (
            "<small>Re:MASTER chart readjusted</small>",
            "<sup>Reworked chart</sup>",
            '<span title="Chart replacement">*</span>',
        ):
            with self.subTest(annotation=annotation):
                parsed = parse_version_page(
                    page("<table>" + HEADER + DATE + row(note=annotation) + "</table>"), URL
                )
                self.assertEqual(parsed["rows"], [])
                self.assertTrue(
                    any(i.startswith("excluded_song_annotation:") for i in parsed["issues"])
                )
        dated = (
            "<table>"
            + HEADER
            + '<tr><th colspan="99" id="20220915">Readjusted Charts</th></tr>'
            + row()
            + "</table>"
        )
        self.assertEqual(parse_version_page(page(dated), URL)["rows"], [])

    def test_unknown_descendant_heading_section_and_caption_fail_closed(self):
        table = "<table>" + HEADER + DATE + row() + "</table>"
        cases = [
            ("<h3>Unrecognized new event</h3>" + table, "unsupported_new_songs_heading:"),
            (
                "<table>"
                + HEADER
                + DATE
                + '<tr><th colspan="99">Unrecognized category</th></tr>'
                + row()
                + "</table>",
                "unsupported_table_section:",
            ),
            (
                table.replace("<table>", "<table><caption>Unrecognized scope</caption>"),
                "unsupported_table_caption:",
            ),
        ]
        for body, reason in cases:
            with self.subTest(reason=reason):
                parsed = parse_version_page(page(body), URL)
                self.assertEqual(parsed["rows"], [])
                self.assertTrue(any(i.startswith(reason) for i in parsed["issues"]))

    def test_conflicting_explicit_chart_formats_do_not_infer_unique_type(self):
        parsed = parse_version_page(
            page(
                "<table>"
                + HEADER
                + DATE
                + row(note="<small>STD Charts / DX Charts</small>")
                + "</table>"
            ),
            URL,
        )
        self.assertEqual(parsed["rows"], [])
        self.assertTrue(
            any(i.startswith("unsupported_or_ambiguous_chart_type:") for i in parsed["issues"])
        )

    def test_unknown_version_name_does_not_alias_latest(self):
        parsed = parse_version_page(
            page("<table>" + HEADER + DATE + row() + "</table>", identity="maimai_DX:UNKNOWN"),
            URL.replace("FESTiVAL", "UNKNOWN"),
        )
        self.assertEqual(parsed["rows"], [])
        self.assertIn("unsupported_or_regional_version_page", parsed["issues"])


if __name__ == "__main__":
    unittest.main()
