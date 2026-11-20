"""A small generated catalog, synthetic AP imports, and catalog pipeline sources.

The 101 filler charts deliberately exceed the API's default 100-row page.
Six hand-authored rows exercise STD/DX identity, Re:MASTER, blank titles,
identical song titles disambiguated by external IDs, and low/high imports.
"""

import copy
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts.catalog_chart_compiler import (
    build,
    load_record_review,
    validate_chart_decisions,
)
from scripts.catalog_pipeline import SOURCES, atomic_json, prepare_update
from scripts.catalog_validate import validate

CHART_COUNT = 107
IMPORTED_COUNT = 6
LEGACY_COMPLETE = 2
LOW_COUNT = 3
HIGH_COMPLETE = 1
DIFFICULTY_NAMES = ("BASIC", "ADVANCED", "EXPERT", "MASTER", "Re:MASTER")
VERSIONS_CN = [
    {"version": 10000, "title": "maimai"},
    {"version": 11000, "title": "maimai PLUS"},
    {"version": 12000, "title": "GreeN"},
    {"version": 13000, "title": "GreeN PLUS"},
    {"version": 14000, "title": "ORANGE"},
    {"version": 15000, "title": "ORANGE PLUS"},
    {"version": 16000, "title": "PiNK"},
    {"version": 17000, "title": "PiNK PLUS"},
    {"version": 18000, "title": "MURASAKi"},
    {"version": 18500, "title": "MURASAKi PLUS"},
    {"version": 19000, "title": "MiLK"},
    {"version": 19500, "title": "MiLK PLUS"},
    {"version": 19900, "title": "FiNALE"},
    {"version": 20000, "title": "舞萌DX"},
    {"version": 21000, "title": "舞萌DX 2021"},
    {"version": 22000, "title": "舞萌DX 2022"},
    {"version": 23000, "title": "舞萌DX 2023"},
    {"version": 24000, "title": "舞萌DX 2024"},
    {"version": 25000, "title": "舞萌DX 2025"},
    {"version": 25500, "title": "舞萌DX 2026"},
]


def write_catalog(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    songs = [
        (22, "Fixture ReMASTER", "Test Artist"),
        (30, "Fixture Low", "Test Artist"),
        (131, "Link", "Test Artist A"),
        (383, "Link", "Test Artist B"),
        (1422, "", "Test Artist"),
    ]
    charts = [
        (22, "standard", 4, 13.3, "Fixture ReMASTER"),
        (30, "standard", 0, 4.0, "Fixture Low"),
        (30, "dx", 0, 5.0, "Fixture Low"),
        (131, "standard", 3, 13.1, "Link"),
        (383, "standard", 3, 13.2, "Link(CoF)"),
        (1422, "dx", 0, 6.0, ""),
    ]
    for offset in range(101):
        sid = 2000 + offset
        songs.append((sid, f"Generated song {offset}", "Fixture Artist"))
        charts.append((sid, "standard", 3, 14.0 + (offset % 10) / 10, f"Generated song {offset}"))
    aliases = {30: ["Fixture alias"]}
    with closing(sqlite3.connect(directory / "catalog.sqlite")) as conn, conn:
        conn.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value_json TEXT);
            CREATE TABLE charts(id INTEGER, difficulty INTEGER, type TEXT, data_json TEXT,
                PRIMARY KEY(id, difficulty));
        """)
        conn.execute(
            "INSERT INTO metadata VALUES (?,?)",
            (
                "versions_cn",
                json.dumps(VERSIONS_CN),
            ),
        )
        conn.execute(
            "INSERT INTO metadata VALUES (?,?)", ("snapshot_at", json.dumps("2020-01-01T00:00:00Z"))
        )
        artists = {sid: artist for sid, _, artist in songs}
        titles = {sid: title for sid, title, _ in songs}
        for sid, kind, difficulty, constant, external_title in charts:
            chart_id = sid + (10000 if kind == "dx" else 0)
            chart = {
                "id": chart_id,
                "difficulty": difficulty,
                "lxns_song_id": sid,
                "type": kind,
                "title": titles[sid],
                "artist": artists[sid],
                "difficulty_name": DIFFICULTY_NAMES[difficulty],
                "level": str(int(constant)) + ("+" if constant % 1 >= 0.6 else ""),
                "constant": constant,
                "divingfish_id": str(chart_id),
                "cn_version": 20000,
                "api_version": 20000,
                "jp_release_version": "PiNK",
                "release_status": "resolved",
                "admission_status": "admitted",
                "jacket_path": f"jackets/{sid}.png",
                "aliases": aliases.get(sid, []),
                "external_ids": [
                    {"namespace": "divingfish", "id": str(chart_id), "source_title": external_title}
                ],
            }
            conn.execute(
                "INSERT INTO charts VALUES (?,?,?,?)",
                (chart_id, difficulty, kind, json.dumps(chart, ensure_ascii=False)),
            )
    return directory


def write_imports(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    content = {
        "maimai AP - （低难度）谱面表.tsv": [
            ["4.0", "", "5.0", "", "6.0", ""],
            ["", "", "", "", "", ""],
            ["[BAS, SD] Fixture Low", "", "[BAS, DX] Fixture Low", "", "[BAS, DX] ", "done"],
        ],
        "maimai AP - （高难度）谱面表.tsv": [
            ["13.3", "", "13.1", "", "13.2", ""],
            ["", "", "", "", "", ""],
            [
                "[ReM, SD] Fixture ReMASTER",
                "Fixture Arcade",
                "[MAS, SD] Link",
                "",
                "[MAS, SD] Link(CoF)",
                "",
            ],
        ],
        "maimai AP - 等级完成进度.tsv": [["Fixture summary"]],
        "maimai AP - 汇总表.tsv": [["Fixture summary"]],
    }
    for name, rows in content.items():
        with (directory / name).open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, delimiter="\t").writerows(rows)
    return directory


# Raw source records for the catalog pipeline (落雪 / 水鱼 / arcade-songs).


def song(sid=1, constant=13.0, remaster=False):
    chart = {
        "type": "standard",
        "difficulty": 4 if remaster else 3,
        "level": "13",
        "level_value": constant,
        "note_designer": "Designer",
        "version": 10000,
        "notes": {"total": 1, "tap": 1, "hold": 0, "slide": 0, "touch": 0, "break": 0},
    }
    return {
        "id": sid,
        "title": f"Song {sid}",
        "artist": "Artist",
        "genre": "maimai",
        "bpm": 120,
        "version": 10000,
        "difficulties": {"standard": [chart], "dx": []},
    }


def document(songs):
    return {"songs": songs, "versions": [{"version": 10000, "title": "maimai"}], "genres": []}


def fish(sid=1, constant=13.0, level="13", sheets=4, charter="Designer", notes=None, **info):
    """A 水鱼 SD song whose MASTER (and Re:MASTER with sheets=5) carry ``constant``."""
    title = info.pop("title", f"Song {sid}")
    return {
        "id": str(sid),
        "title": title,
        "type": "SD",
        "ds": [1, 2, 3, constant, constant][:sheets],
        "level": ["1", "2", "3", level, level][:sheets],
        "basic_info": {"title": title, "artist": "Artist", "bpm": 120.0, **info},
        "charts": [{"notes": notes or [1, 0, 0, 0], "charter": charter}] * sheets,
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


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def png():
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(stream, "PNG")
    return stream.getvalue()


class PipelineCase(unittest.TestCase):
    """A previous snapshot of Song 1 (MASTER) and Song 2 (Re:MASTER), hardlinked into a stage.

    ``self.responses`` holds what the fake network returns per source file; an
    exception value makes that fetch fail. Every other URL serves a jacket PNG.
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.previous = Path(temp.name) / "previous"
        self.stage = Path(temp.name) / "stage"
        self.old = document([song(1), song(2, remaster=True)])
        sources = {
            "lxns_songs.json": self.old,
            "lxns_aliases.json": {"aliases": []},
            "divingfish_music_data.json": [],
            "arcade_songs.json": {"songs": []},
        }
        receipts = []
        for name, value in sources.items():
            path = self.previous / "raw" / name
            atomic_json(path, value)
            receipts.append(
                {
                    "file": name,
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                    "fetched_at": "2020-01-01T00:00:00Z",
                }
            )
        atomic_json(self.previous / "raw/sources.json", receipts)
        atomic_json(self.previous / "remy_release_overrides.json", {"overrides": []})
        shutil.copytree(self.previous, self.stage, copy_function=os.link)
        self.responses = copy.deepcopy(sources)
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        for name, source_url, *_ in SOURCES:
            if url == source_url:
                value = self.responses[name]
                if isinstance(value, Exception):
                    raise value
                return json.dumps(value).encode(), {}
        return png(), {}

    def prepare(self, fetch=None):
        with (
            patch("scripts.catalog_pipeline.fetch", side_effect=fetch or self.fetch),
            patch("scripts.catalog_pipeline.time.sleep"),
        ):
            return prepare_update(self.stage, self.previous)

    def read(self, name):
        return json.loads((self.stage / name).read_text())

    def compile(self):
        build(self.stage)
        return validate(self.stage)

    def charts(self):
        return {chart["chart_key"]: chart for chart in self.read("catalog.json")["charts"]}

    def update_sources(self, lxns, divingfish):
        """Fetch and compile new sources; return published charts and review rows."""
        self.responses["lxns_songs.json"] = document(lxns)
        self.responses["divingfish_music_data.json"] = divingfish
        self.prepare()
        self.compile()
        review = self.read("catalog_record_review.json")["records"]
        return self.charts(), {(r["id"], r["difficulty"]): r for r in review}

    def with_comparable_sources(self):
        """水鱼 disagrees with 落雪 on Song 1's artist, constant and level."""
        self.responses["arcade_songs.json"] = arcade(title="Song 1")
        self.responses["divingfish_music_data.json"] = [
            fish(1, 14.4, level="14", charter="-", artist="Other Artist", genre="舞萌"),
            fish(2, 13.0, sheets=5),
        ]
        self.prepare()
        build(self.stage)

    def review(self):
        return load_record_review(self.stage)

    def record(self, ident=1, difficulty=3):
        rows = self.review()["records"]
        return next(r for r in rows if (r["id"], r["difficulty"]) == (ident, difficulty))

    def choose(self, ident=1, difficulty=3, **values):
        """Choose every pending field: the option with ``values[field]``, else 水鱼's, else the first."""
        review = self.review()
        selected = []
        for field in self.record(ident, difficulty)["fields"]:
            options = field["options"]
            if field["field_key"] in values:
                option = next(o for o in options if o["value"] == values[field["field_key"]])
            else:
                option = next((o for o in options if o["source"] == "divingfish"), options[0])
            selected.append({"field_key": field["field_key"], "option_id": option["option_id"]})
        return validate_chart_decisions(review, ident, difficulty, selected)

    def adjudicate(self, decisions, revision=1):
        atomic_json(
            self.stage / "catalog_adjudications.json",
            {"schema_version": 2, "revision": revision, "decisions": decisions},
        )

    def rehash_source(self, name, value):
        """Replace a raw source and its receipt, as a later fetch would."""
        atomic_json(self.stage / "raw" / name, value)
        receipts = self.read("raw/sources.json")
        for row in receipts:
            if row["file"] == name:
                path = self.stage / "raw" / name
                row.update(sha256=sha256(path), bytes=path.stat().st_size)
        atomic_json(self.stage / "raw/sources.json", receipts)
