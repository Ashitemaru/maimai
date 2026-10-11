"""Extract Japanese Re:MASTER debuts from saved Remy version-page HTML.

This module does not fetch pages or match songs to a catalog. A populated
Re:MASTER cell is evidence only inside a dated New Songs table, never in the
page's difficulty-change, revival, removal, or regional-release tables.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

_VERSIONS = (
    "maimai",
    "maimai PLUS",
    "GreeN",
    "GreeN PLUS",
    "ORANGE",
    "ORANGE PLUS",
    "PiNK",
    "PiNK PLUS",
    "MURASAKi",
    "MURASAKi PLUS",
    "MiLK",
    "MiLK PLUS",
    "FiNALE",
    "DX",
    "DX PLUS",
    "Splash",
    "Splash PLUS",
    "UNiVERSE",
    "UNiVERSE PLUS",
    "FESTiVAL",
    "FESTiVAL PLUS",
    "BUDDiES",
    "BUDDiES PLUS",
    "PRiSM",
    "PRiSM PLUS",
    "CiRCLE",
    "CiRCLE PLUS",
    "MAGiCAL",
    "MAGiCAL PLUS",
)
_CANONICAL = {v.casefold(): v for v in _VERSIONS}
_OLD_VERSIONS = set(_VERSIONS[:13])
_NEW_SONGS = {"new songs", "new songs / dx charts"}
_EXCLUDED = re.compile(
    r"difficulty changes?|changed levels?|removed songs?|revived songs?|all songs?|"
    r"song list|international|overseas|\basia\b|\bchina\b|\bkorea\b|\btaiwan\b|"
    r"hong kong|中国|國際|国际|海外|\breadjust(?:ed|ments?)?\b|\brework(?:ed|s)?\b|"
    r"\breplac(?:ed|ement)s?\b|\brevis(?:ed|ions?)\s+charts?\b|譜面変更|譜面差し替え",
    re.I,
)
_GENRES = {
    "pops & anime",
    "niconico & vocaloid",
    "東方project",
    "toho project",
    "game & variety",
    "variety",
    "original",
    "original & joypolis",
    "maimai",
    "ongeki & chunithm",
    "sega",
    "宴会場",
    "utage",
}
_JAPAN = re.compile(r"Japan(?:ese)?(?:\s+(?:Release|Version|Songs))?", re.I)
_SECTION = re.compile(
    r"Unlockable (?:in|by) .+|Unlocked by playing .+|Stamp Card unlock|"
    r"Challenge Track #\d+|Survival Course - .+|\d{4} Boss Song Crossover|ONGEKI collaboration|"
    r"Unlocked with maiberry|Must play one round of .+ to access|"
    r"AOU member arcades only, unlocked for everyone on .+|"
    r"Event Course(?: unlock)?; .+|[\d,]+ maiZENNY gift",
    re.I,
)
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def _clean(value: str) -> str:
    return " ".join(value.split())


def _known_section(text: str) -> bool:
    return text.casefold() in _GENRES or bool(_JAPAN.fullmatch(text) or _SECTION.fullmatch(text))


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list = field(default_factory=list)

    def text(self, *, title: bool = False) -> str:
        if self.tag in {"script", "style", "sup"}:
            return ""
        if title and self.tag == "small":
            return ""
        if self.tag in {"br", "hr"}:
            return " "
        if self.tag == "img":
            return self.attrs.get("alt", "")
        return "".join(c.text(title=title) if isinstance(c, _Node) else c for c in self.children)

    def descendants(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.descendants()


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        # MediaWiki emits explicit end tags; these also tolerate ordinary HTML
        # optional cell/row closing tags without allowing one row to eat another.
        if tag in {"td", "th"} and self.stack[-1].tag in {"td", "th"}:
            self.stack.pop()
        if tag == "tr":
            while self.stack[-1].tag in {"tr", "td", "th"}:
                self.stack.pop()
        node = _Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _version(value: str | None) -> str | None:
    if not value:
        return None
    value = _clean(unquote(value).replace("_", " "))
    value = re.split(r"\s+-\s+(?:SilentBlue\.)?RemyWiki", value, flags=re.I)[0]
    if _EXCLUDED.search(value) or "/" in value:
        return None
    if value.startswith("maimai DX:"):
        value = value.split(":", 1)[1]
        value = {"1st": "DX", "PLUS": "DX PLUS"}.get(value, value)
    elif value.startswith("maimai:"):
        value = value.split(":", 1)[1]
        value = {"1st": "maimai", "PLUS": "maimai PLUS"}.get(value, value)
    elif value.startswith("maimai でらっくす"):
        value = value.removeprefix("maimai でらっくす").strip()
        value = {"": "DX", "PLUS": "DX PLUS"}.get(value, value)
    elif value.startswith("maimai ") and value.casefold() != "maimai plus":
        value = value.removeprefix("maimai ")
    return _CANONICAL.get(value.casefold())


def _source_page(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname == "web.archive.org":
        match = re.match(r"/web/\d+(?:[a-z_]+)?/(https?://.+)", parsed.path)
        if not match:
            return None
        parsed = urlsplit(match.group(1) + ("?" + parsed.query if parsed.query else ""))
    if parsed.scheme not in {"https", "http"} or not (
        parsed.hostname == "remywiki.com" or (parsed.hostname or "").endswith(".remywiki.com")
    ):
        return None
    page = parse_qs(parsed.query).get("title", [unquote(parsed.path).strip("/")])[0]
    return page.removeprefix("wiki/")


def _config_string(html: str, key: str) -> str | None:
    found = re.search(r'"' + key + r'"\s*:\s*("(?:[^"\\]|\\.)*")', html)
    return json.loads(found.group(1)) if found else None


def _date(node: _Node) -> tuple[str | None, bool]:
    # Date anchor IDs are authoritative even when the visible heading is Launch.
    for child in node.descendants():
        value = child.attrs.get("id", "")
        if re.fullmatch(r"\d{8}(?:_\d+)?", value):
            try:
                return datetime.strptime(value[:8], "%Y%m%d").date().isoformat(), True
            except ValueError:
                return None, True
    value = _clean(node.text())
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat(), True
        except ValueError:
            return None, True
    value = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value)
    match = re.fullmatch(r"(?:Launch\s*\()?([A-Za-z]+ \d{1,2}, \d{4})\)?", value)
    if match:
        try:
            return datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat(), True
        except ValueError:
            return None, True
    return None, False


def _events(node: _Node):
    if node.tag == "table" or re.fullmatch(r"h[1-6]", node.tag):
        yield node
        return
    for child in node.children:
        if isinstance(child, _Node):
            yield from _events(child)


def _rows(table: _Node):
    for child in table.children:
        if not isinstance(child, _Node):
            continue
        if child.tag == "tr":
            yield child
        elif child.tag in {"tbody", "thead", "tfoot"}:
            yield from _rows(child)


def _span(cell: _Node, attr: str) -> int:
    try:
        return max(1, min(int(cell.attrs.get(attr, "1")), 100))
    except ValueError:
        return 1


def _chart_type(title_cell: _Node, format_cell: _Node | None, version: str) -> str | None:
    values = []
    if format_cell is not None:
        values.append(format_cell.text())
    for node in title_cell.descendants():
        if node.tag == "img":
            values.extend([node.attrs.get("alt", ""), node.attrs.get("title", "")])
        elif node.tag == "small":
            values.append(node.text())
    text = " ".join(values)
    notes = title_cell.text()
    standard = bool(
        re.search(r"\b(?:standard|std)\s+charts?\b", notes, re.I)
        or re.search(r"\b(?:standard|std)\b", text, re.I)
    )
    dx = bool(re.search(r"\bDX\s+Charts?\b", notes, re.I) or re.search(r"\bDX\b", text, re.I))
    if standard and dx:
        return "invalid"
    if standard:
        return "standard"
    if dx:
        return "dx"
    if format_cell is not None and _clean(format_cell.text()):
        return "invalid"
    return "standard" if version in _OLD_VERSIONS else None


def _parse_table(table: _Node, version: str, initial_date: str | None, issues: list[str]):
    records = []
    columns: dict[str, int] = {}
    spans: dict[int, tuple[_Node, int]] = {}
    date = initial_date
    valid_header = False
    header_seen = False
    regional = False
    utage = False
    unsupported = False
    for row in _rows(table):
        cells = [c for c in row.children if isinstance(c, _Node) and c.tag in {"td", "th"}]
        if not cells:
            continue
        # Scope restrictions still apply when a dated section embeds an anchor.
        # Checking the date first would turn a dated chart-rework heading into
        # an ordinary debut date for all following rows.
        if all(c.tag == "th" for c in cells) and _EXCLUDED.search(_clean(row.text())):
            regional = True
            spans = {}
            continue
        row_date, is_date = _date(row) if all(c.tag == "th" for c in cells) else (None, False)
        if is_date:
            date = row_date
            if date is None:
                issues.append("invalid_date_heading")
            spans = {}
            utage = False
            unsupported = False
            continue
        # Section rows commonly use colspan=99 instead of the table width.
        if len(cells) == 1 and _span(cells[0], "colspan") > 1:
            text = _clean(cells[0].text())
            utage = bool(re.search(r"宴会場|\bUTAGE\b", text, re.I))
            if _EXCLUDED.search(text):
                regional = True
            elif _JAPAN.fullmatch(text):
                regional = False
            unsupported = not _known_section(text) and not _EXCLUDED.search(text)
            if unsupported:
                issues.append("unsupported_table_section: " + text)
            spans = {}
            continue
        expanded = {i: cell for i, (cell, _) in spans.items()}
        next_spans = {i: (cell, left - 1) for i, (cell, left) in spans.items() if left > 1}
        cursor = 0
        for cell in cells:
            while cursor in expanded:
                cursor += 1
            for col in range(cursor, cursor + _span(cell, "colspan")):
                expanded[col] = cell
                if _span(cell, "rowspan") > 1:
                    next_spans[col] = (cell, _span(cell, "rowspan") - 1)
            cursor += _span(cell, "colspan")
        spans = next_spans
        if all(c.tag == "th" for c in cells):
            if any(re.sub(r"\s", "", c.text()).casefold() in {"song", "songtitle"} for c in cells):
                columns = {}
            for col, cell in sorted(expanded.items()):
                name = re.sub(r"[\s:]", "", cell.text()).casefold()
                key = {
                    "song": "title",
                    "songtitle": "title",
                    "artist": "artist",
                    "remaster": "remaster",
                    "format": "format",
                    "type": "format",
                    "charttype": "format",
                }.get(name)
                if key and (key not in columns or expanded.get(columns[key]) is not cell):
                    columns[key] = col
            valid_header = {"title", "artist", "remaster"}.issubset(columns)
            header_seen |= valid_header
            continue
        if regional or utage or unsupported:
            continue
        if not valid_header:
            if any(c.tag == "td" for c in cells):
                issues.append("unrecognized_new_songs_table_header")
            continue
        if any(columns[key] not in expanded for key in ("title", "artist", "remaster")):
            issues.append("incomplete_song_row")
            continue
        level = _clean(expanded[columns["remaster"]].text())
        if level in {"", "-", "–", "—", "―", "N/A"}:
            continue
        if not re.fullmatch(r"\d{1,2}\+?", level):
            issues.append("invalid_remaster_level: " + level)
            continue
        title_cell = expanded[columns["title"]]
        title = _clean(title_cell.text(title=True))
        title = re.sub(r"\s+(?:DX|Standard|STD) Charts?$", "", title, flags=re.I)
        # A populated cell after a rework is not evidence of the first debut.
        # Inspect annotations too (text(title=True) intentionally hides them).
        annotations = (
            " ".join(
                child if isinstance(child, str) else ""
                for node in row.descendants()
                for child in node.children
            )
            + " "
            + " ".join(n.attrs.get("title", "") for n in row.descendants())
        )
        if _EXCLUDED.search(annotations):
            issues.append("excluded_song_annotation: " + title)
            continue
        if not title or not date:
            issues.append("missing_title_or_date: " + title)
            continue
        format_cell = expanded.get(columns.get("format", -1))
        if format_cell is None and columns["artist"] - columns["title"] > 1:
            possible_format = expanded.get(columns["title"] + 1)
            if possible_format is not title_cell:
                format_cell = possible_format
        chart_type = _chart_type(title_cell, format_cell, version)
        if chart_type == "invalid":
            issues.append("unsupported_or_ambiguous_chart_type: " + title)
            continue
        records.append(
            {
                "title": title,
                "artist": _clean(expanded[columns["artist"]].text()),
                "chart_type": chart_type,
                "date": date,
                "version": version,
                "remaster_level": level,
                "evidence_row": [
                    _clean(c.text())
                    for i, c in sorted(expanded.items())
                    if i == 0 or expanded.get(i - 1) is not c
                ],
            }
        )
    return records, header_seen


def parse_version_page(html: str, source_url: str, *, version: str | None = None) -> dict:
    """Return extracted rows and explicit issues; never perform network I/O.

    ``issues`` is a list of stable, human-readable strings. Identity/metadata
    failures return no rows. Row-level issues can accompany otherwise valid
    rows. An empty rows list with no issues means a recognized table contains
    no Re:MASTER additions, rather than a fetch/layout failure.
    """
    result = {
        "source_url": source_url,
        "source_revision": None,
        "version": None,
        "rows": [],
        "issues": [],
    }
    issues = result["issues"]
    page = _source_page(source_url)
    if page is None:
        issues.append("not_remy_source_url")
        return result
    if not html.strip():
        issues.append("empty_html")
        return result
    if re.search(
        r"<title[^>]*>\s*(?:Just a moment|Access denied|Forbidden|Attention Required)|cf-chl-|challenge-platform|captcha-container",
        html,
        re.I,
    ):
        issues.append("access_challenge_page")
        return result
    parser = _Document()
    parser.feed(html)
    parser.close()
    nodes = list(parser.root.descendants())
    title = next((_clean(n.text()) for n in nodes if n.tag == "title"), "")
    config_page = _config_string(html, "wgPageName")
    revision = re.search(r'"wgRevisionId"\s*:\s*(\d+)', html) or re.search(
        r'"wgCurRevisionId"\s*:\s*(\d+)', html
    )
    if revision:
        result["source_revision"] = revision.group(1)
    if not config_page and "remywiki" not in title.casefold():
        issues.append("not_remy_article_html")
        return result
    inferred = _version(page)
    if not inferred:
        issues.append("unsupported_or_regional_version_page")
        return result
    result["version"] = inferred
    if config_page and _version(config_page) != inferred:
        issues.append("page_version_metadata_mismatch")
        return result
    title_version = _version(title)
    if title_version and title_version != inferred:
        issues.append("page_title_version_mismatch")
        return result
    if version is not None and _version(version) != inferred:
        issues.append("requested_version_mismatch")
        return result
    outline: dict[int, tuple[str, bool]] = {}
    date = None
    section_seen = False
    header_seen = False
    for event in _events(parser.root):
        if event.tag.startswith("h"):
            depth = int(event.tag[1])
            outline = {k: v for k, v in outline.items() if k < depth}
            heading = _clean(event.text()).removesuffix("[edit]").strip()
            heading_date, is_date = _date(event)
            outline[depth] = (heading, is_date)
            if heading.casefold() in _NEW_SONGS:
                date = None
                section_seen = True
            if is_date:
                date = heading_date
                if date is None:
                    issues.append("invalid_date_heading")
            continue
        context = [heading for heading, _ in outline.values()]
        if not any(h.casefold() in _NEW_SONGS for h in context) or any(
            _EXCLUDED.search(h) for h in context
        ):
            continue
        new_depth = max(
            depth for depth, (heading, _) in outline.items() if heading.casefold() in _NEW_SONGS
        )
        unknown = [
            heading
            for depth, (heading, dated) in outline.items()
            if depth > new_depth and not dated and not _known_section(heading)
        ]
        if unknown:
            issues.extend("unsupported_new_songs_heading: " + heading for heading in unknown)
            continue
        if any(re.search(r"宴会場|\bUTAGE\b", h, re.I) for h in context):
            continue
        caption = " ".join(
            _clean(c.text()) for c in event.children if isinstance(c, _Node) and c.tag == "caption"
        )
        if _EXCLUDED.search(caption):
            continue
        if caption and not _known_section(caption):
            issues.append("unsupported_table_caption: " + caption)
            continue
        rows, recognized = _parse_table(event, inferred, date, issues)
        result["rows"].extend(rows)
        header_seen |= recognized
    if not section_seen:
        issues.append("missing_new_songs_section")
    elif not header_seen:
        issues.append("missing_new_songs_table_header")
    result["issues"] = list(dict.fromkeys(issues))
    return result
