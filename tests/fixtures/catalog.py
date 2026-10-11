"""A small generated legacy catalog and synthetic AP imports.

The 101 filler charts deliberately exceed the API's default 100-row page.
Six hand-authored rows exercise STD/DX identity, Re:MASTER, blank titles,
identical song titles disambiguated by external IDs, and low/high imports.
"""

import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

CHART_COUNT = 107
IMPORTED_COUNT = 6
LEGACY_COMPLETE = 2
LOW_COUNT = 3
HIGH_COMPLETE = 1


def write_catalog(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    versions = [
        (10000, "maimai"),
        (11000, "maimai PLUS"),
        (12000, "GreeN"),
        (13000, "GreeN PLUS"),
        (14000, "ORANGE"),
        (15000, "ORANGE PLUS"),
        (16000, "PiNK"),
        (17000, "PiNK PLUS"),
        (18000, "MURASAKi"),
        (18500, "MURASAKi PLUS"),
        (19000, "MiLK"),
        (19500, "MiLK PLUS"),
        (19900, "FiNALE"),
        (20000, "舞萌DX"),
        *[(20000 + (year - 2020) * 1000, f"舞萌DX {year}") for year in range(2021, 2026)],
        (25500, "舞萌DX 2026"),
    ]
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
    with closing(sqlite3.connect(directory / "catalog.sqlite")) as conn, conn:
        conn.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value_json TEXT);
            CREATE TABLE songs(id INTEGER PRIMARY KEY,title TEXT,artist TEXT);
            CREATE TABLE charts(chart_key TEXT PRIMARY KEY,song_id INTEGER,type TEXT,
                difficulty_index INTEGER,difficulty_name TEXT,level TEXT,constant REAL,
                divingfish_id TEXT,api_version INTEGER,jp_release_version TEXT,
                jp_release_version_candidate TEXT,release_status TEXT);
            CREATE TABLE aliases(song_id INTEGER,alias TEXT);
            CREATE TABLE external_ids(namespace TEXT,external_id TEXT,song_id INTEGER,
                type TEXT,mapping_rule TEXT,source_title TEXT);
        """)
        conn.execute(
            "INSERT INTO metadata VALUES (?,?)",
            (
                "versions_cn",
                json.dumps([{"version": version, "title": title} for version, title in versions]),
            ),
        )
        conn.execute(
            "INSERT INTO metadata VALUES (?,?)", ("snapshot_at", json.dumps("2020-01-01T00:00:00Z"))
        )
        conn.executemany("INSERT INTO songs VALUES (?,?,?)", songs)
        for sid, kind, difficulty, constant, external_title in charts:
            external_id = str(sid + (10000 if kind == "dx" else 0))
            level = str(int(constant)) + ("+" if constant % 1 >= 0.6 else "")
            conn.execute(
                "INSERT INTO charts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{sid}:{kind}:{difficulty}",
                    sid,
                    kind,
                    difficulty,
                    ("BASIC", "ADVANCED", "EXPERT", "MASTER", "Re:MASTER")[difficulty],
                    level,
                    constant,
                    external_id,
                    20000,
                    "PiNK",
                    None,
                    "resolved",
                ),
            )
            conn.execute(
                "INSERT INTO external_ids VALUES (?,?,?,?,?,?)",
                ("divingfish", external_id, sid, kind, "fixture_id", external_title),
            )
        conn.execute("INSERT INTO aliases VALUES (?,?)", (30, "Fixture alias"))
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
