"""Catalog is read-only; personal observations live in a separate SQLite database."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIFFICULTIES = {"BAS": 0, "ADV": 1, "EXP": 2, "MAS": 3, "ReM": 4}
DEFAULT_PREFERENCES = {"default_constant_min": 13.0, "default_constant_max": 13.9}


class ManualConflictError(Exception):
    """A form is based on an older version of the manual record."""


class SyncBusyError(Exception):
    """Only one provider request may be in flight for this account."""


class SyncTokenChangedError(Exception):
    """A provider response belongs to credentials that have since changed."""


CN_VERSION_SHORT_LABELS = {
    "maimai": "初",
    "maimai PLUS": "真",
    "GreeN": "超",
    "GreeN PLUS": "檄",
    "ORANGE": "橙",
    "ORANGE PLUS": "晓",
    "PiNK": "桃",
    "PiNK PLUS": "樱",
    "MURASAKi": "紫",
    "MURASAKi PLUS": "堇",
    "MiLK": "白",
    "MiLK PLUS": "雪",
    "FiNALE": "辉",
    "DX": "熊华",
    "DX2021": "爽煌",
    "DX2022": "宙星",
    "DX2023": "祭祝",
    "DX2024": "双宴",
    "DX2025": "镜",
    "DX2026": "彩",
}


def now():
    return datetime.now(UTC).isoformat()


def normalized(value):
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def canonical_chart_key(value):
    """Translate a historical key without changing a chart's identity."""
    match = re.fullmatch(r"(\d+):(standard|dx|utage):(\d+)", str(value))
    if not match:
        return str(value)
    song_id, kind, difficulty = match.groups()
    return f"{int(song_id) + (10000 if kind == 'dx' else 0)}:{int(difficulty)}"


class ChartIndex(dict):
    schema_version = 1

    def __init__(self, *args, schema_version=1, source_charts=None, versions_cn=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.schema_version = schema_version
        self.source_charts = source_charts if source_charts is not None else self
        self.cn_version_ranges = []
        self.cn_versions = []
        self.cn_matrix_versions = []
        versions = sorted(versions_cn, key=lambda row: row["version"])
        for index, row in enumerate(versions):
            start = row["version"]
            if start < 10000:
                continue
            title = row["title"]
            dx_title = re.fullmatch(r"(?:舞萌)?DX\s*(20\d{2})?", title)
            label = "DX" + (dx_title[1] or "") if dx_title else title
            # Unknown later releases must not inherit the last known year's label.
            stop = (
                versions[index + 1]["version"]
                if index + 1 < len(versions)
                else (start // 1000 + 1) * 1000
            )
            self.cn_version_ranges.append((start, stop))
            self.cn_versions.append({"value": str(start), "label": label})
            self.cn_matrix_versions.append(
                {
                    "value": str(start),
                    "label": CN_VERSION_SHORT_LABELS.get(label, label),
                    "title": title,
                }
            )

    def cn_version_group(self, value):
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        return next(
            (start for start, stop in self.cn_version_ranges if start <= value < stop), None
        )


class ChartList(list):
    """Chart rows and filter metadata from one published catalog snapshot."""

    def __init__(self, *args, cn_versions=(), cn_matrix_versions=()):
        super().__init__(*args)
        self.cn_versions = cn_versions
        self.cn_matrix_versions = cn_matrix_versions


def requires_venue(chart, legacy):
    return legacy["group_name"] == "high" if legacy else chart["constant"] >= 12.6


class Store:
    def __init__(self, runtime=None, import_dir=None, catalog_dir=None):
        self.runtime = Path(
            runtime or os.environ.get("MAIMAI_RUNTIME_DIR", ROOT / "backend/runtime")
        )
        self.db_path = self.runtime / "personal.sqlite3"
        self.secret_path = self.runtime / "secrets.json"
        self.import_dir = Path(
            import_dir or os.environ.get("MAIMAI_IMPORT_DIR", Path.home() / "Downloads")
        )
        self.catalog_root = Path(
            catalog_dir or os.environ.get("MAIMAI_CATALOG_DIR", ROOT / "data/catalog")
        ).resolve()
        self._token_lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._token_generation = 0
        if os.environ.get("MAIMAI_ENV") == "production":
            if not (self.catalog_directory() / "catalog.sqlite").is_file():
                raise RuntimeError("Production catalog.sqlite is missing; set MAIMAI_CATALOG_DIR.")
            if not self.db_path.is_file():
                raise RuntimeError(
                    "Production personal.sqlite3 is missing; restore MAIMAI_RUNTIME_DIR."
                )
            try:
                with self.db() as conn:
                    imported = conn.execute(
                        "SELECT value FROM metadata WHERE key='import'"
                    ).fetchone()
                if not imported or not isinstance(json.loads(imported[0]), dict):
                    raise ValueError("missing import metadata")
            except (sqlite3.Error, ValueError) as exc:
                raise RuntimeError(
                    "Production personal.sqlite3 has no valid import metadata; restore the existing personal database."
                ) from exc
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialized = False
        self.reload_catalog()
        if os.environ.get("MAIMAI_ENV") == "production" and not self.catalog:
            raise RuntimeError("Production catalog has no admitted charts.")
        self._initialize()
        self._initialized = True

    @property
    def catalog(self):
        return self._catalog_snapshot[0]

    @property
    def waterfish_map(self):
        return self._catalog_snapshot[1]

    def catalog_directory(self):
        current = self.catalog_root / "current"
        return current.resolve() if current.exists() else self.catalog_root

    def install_catalog(self, loaded_tuple):
        if self._initialized and getattr(loaded_tuple[0], "schema_version", 1) >= 2:
            with self.db() as conn:
                self._migrate_personal_keys(conn)
        # Publish both indexes together; readers retain their existing snapshot.
        self._catalog_snapshot = tuple(loaded_tuple)

    def reload_catalog(self):
        self.install_catalog(self.load_catalog(self.catalog_directory()))

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def load_catalog(self, path):
        conn = sqlite3.connect(
            f"{(Path(path) / 'catalog.sqlite').resolve().as_uri()}?mode=ro", uri=True
        )
        conn.row_factory = sqlite3.Row
        try:
            version_row = conn.execute(
                "SELECT value_json FROM metadata WHERE key='versions_cn'"
            ).fetchone()
            versions_cn = json.loads(version_row[0]) if version_row else []
            columns = {row[1] for row in conn.execute("PRAGMA table_info(charts)")}
            if {"id", "difficulty"} <= columns:
                return self._load_chart_catalog(conn, Path(path), versions_cn)
            aliases = {}
            for row in conn.execute("SELECT song_id,alias FROM aliases"):
                aliases.setdefault(row[0], []).append(row[1])
            charts = ChartIndex(versions_cn=versions_cn)
            for row in conn.execute(
                "SELECT c.*,s.title,s.artist FROM charts c JOIN songs s ON s.id=c.song_id WHERE c.type!='utage'"
            ):
                c = dict(row)
                c["display_title"] = c["title"] if c["title"].strip() else "（空白曲名）"
                c["jacket_url"] = f"/jackets/{c['song_id']}.png"
                c["_search"] = normalized(
                    " ".join([c["title"], c["artist"], *aliases.get(c["song_id"], [])])
                )
                charts[c["chart_key"]] = c
            mapping = {
                str(r["external_id"]): dict(r)
                for r in conn.execute("SELECT * FROM external_ids WHERE namespace='divingfish'")
            }
        finally:
            conn.close()
        return charts, mapping

    def _load_chart_catalog(self, conn, directory, versions_cn):
        source_charts, mapping = self._source_chart_index(directory)
        charts = ChartIndex(schema_version=2, source_charts=source_charts, versions_cn=versions_cn)
        for row in conn.execute("SELECT data_json FROM charts WHERE type!='utage'"):
            value = json.loads(row[0])
            chart_id, difficulty = int(value["id"]), int(value["difficulty"])
            if (
                value.get("admission_status") != "admitted"
                or value.get("release_status") == "conflict"
                or value.get("status") in {"pending", "conflict"}
            ):
                raise ValueError("正式曲库包含未裁决谱面")
            value.update(
                chart_key=f"{chart_id}:{difficulty}", song_id=chart_id, difficulty_index=difficulty
            )
            value["display_title"] = value["title"] if value["title"].strip() else "（空白曲名）"
            value["jacket_url"] = "/" + value["jacket_path"].lstrip("/")
            value["_search"] = normalized(
                " ".join([value["title"], value["artist"], *value.get("aliases", [])])
            )
            charts[value["chart_key"]] = value
            source_charts.setdefault(value["chart_key"], value)
            for external in value.get("external_ids", []):
                if external.get("namespace") == "divingfish":
                    mapping[str(external["id"])] = {
                        "song_id": chart_id,
                        "type": value["type"],
                        "source_title": external.get("source_title", value["title"]),
                    }
            if value.get("divingfish_id"):
                mapping.setdefault(
                    str(value["divingfish_id"]),
                    {"song_id": chart_id, "type": value["type"], "source_title": value["title"]},
                )
        return charts, mapping

    def _source_chart_index(self, directory):
        """Keep score attachments for pending charts outside the published index."""
        path = directory / "effective_lxns_songs.json"
        if not path.exists():
            path = directory / "raw/lxns_songs.json"
        if not path.exists():
            return {}, {}
        songs = json.loads(path.read_text())["songs"]
        fish_path = directory / "raw/divingfish_music_data.json"
        fish = json.loads(fish_path.read_text()) if fish_path.exists() else []
        by_id = {}
        for item in fish:
            by_id.setdefault(str(item["id"]), []).append(item)
        source_charts, mapping = {}, {}
        names = {0: "BASIC", 1: "ADVANCED", 2: "EXPERT", 3: "MASTER", 4: "Re:MASTER"}
        for song in songs:
            for kind, entries in song["difficulties"].items():
                if kind == "utage":
                    continue
                chart_id = int(song["id"]) + (10000 if kind == "dx" else 0)
                candidates = [
                    item
                    for item in by_id.get(str(chart_id), [])
                    if item.get("type") == ("DX" if kind == "dx" else "SD")
                ]
                source_title = candidates[0]["title"] if len(candidates) == 1 else song["title"]
                mapping[str(chart_id)] = {
                    "song_id": chart_id,
                    "type": kind,
                    "source_title": source_title,
                }
                for entry in entries:
                    difficulty = int(entry["difficulty"])
                    key = f"{chart_id}:{difficulty}"
                    source_charts[key] = {
                        "id": chart_id,
                        "difficulty": difficulty,
                        "chart_key": key,
                        "song_id": chart_id,
                        "lxns_song_id": song["id"],
                        "type": kind,
                        "difficulty_index": difficulty,
                        "difficulty_name": names.get(difficulty, str(difficulty)),
                        "title": song["title"],
                        "artist": song["artist"],
                        "level": entry["level"],
                        "constant": entry["level_value"],
                        "divingfish_id": str(chart_id),
                    }
        return source_charts, mapping

    def _migrate_personal_keys(self, conn):
        row = conn.execute("SELECT value FROM metadata WHERE key='chart_key_schema'").fetchone()
        if row and json.loads(row[0]) == 2:
            return
        for table in ("legacy", "manual", "provider"):
            rows = conn.execute(f"SELECT chart_key FROM {table}").fetchall()
            seen = set()
            for row in rows:
                old, new = row[0], canonical_chart_key(row[0])
                if new in seen:
                    raise ValueError(f"个人记录主键迁移有重复，原记录已保留：{table} {new}")
                seen.add(new)
            for row in rows:
                old, new = row[0], canonical_chart_key(row[0])
                if new != old:
                    conn.execute(f"UPDATE {table} SET chart_key=? WHERE chart_key=?", (new, old))
        for row in conn.execute(
            "SELECT id,chart_key FROM events WHERE chart_key IS NOT NULL"
        ).fetchall():
            conn.execute(
                "UPDATE events SET chart_key=? WHERE id=?", (canonical_chart_key(row[1]), row[0])
            )
        conn.execute(
            "INSERT INTO metadata VALUES('chart_key_schema','2') ON CONFLICT(key) DO UPDATE SET value='2'"
        )

    def _initialize(self):
        with self.db() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy(
              chart_key TEXT PRIMARY KEY, group_name TEXT NOT NULL, constant REAL NOT NULL,
              complete INTEGER NOT NULL, venue TEXT NOT NULL, label TEXT NOT NULL,
              source_file TEXT NOT NULL, source_row INTEGER NOT NULL, source_col INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS manual(
              chart_key TEXT PRIMARY KEY, ap INTEGER NOT NULL, venue TEXT NOT NULL,
              achievement REAL, note TEXT NOT NULL, achieved_date TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS provider(
              chart_key TEXT PRIMARY KEY, achievement REAL, fc TEXT NOT NULL,
              synced_at TEXT NOT NULL, raw_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, chart_key TEXT, kind TEXT NOT NULL,
              created_at TEXT NOT NULL, data_json TEXT NOT NULL);
            """)
            if not conn.execute("SELECT 1 FROM metadata WHERE key='import' ").fetchone():
                self._import(conn)
            if getattr(self.catalog, "schema_version", 1) >= 2:
                self._migrate_personal_keys(conn)
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('preferences', ?)",
                (json.dumps(DEFAULT_PREFERENCES),),
            )
        self.db_path.chmod(0o600)

    def _import(self, conn):
        source_names = {
            "low": "maimai AP - （低难度）谱面表.tsv",
            "high": "maimai AP - （高难度）谱面表.tsv",
            "levels": "maimai AP - 等级完成进度.tsv",
            "summary": "maimai AP - 汇总表.tsv",
        }
        # The source title disambiguates Link (131) from Link(CoF) (383).
        index = {}
        catalog, waterfish_map = self._catalog_snapshot
        for c in getattr(catalog, "source_charts", catalog).values():
            source_title = waterfish_map.get(str(c["divingfish_id"]), {}).get("source_title")
            name = source_title if source_title is not None else c["title"]
            index.setdefault((normalized(name), c["type"], c["difficulty_index"]), []).append(c)
        sources, records = {}, []
        for group, filename in source_names.items():
            path = self.import_dir / filename
            raw = path.read_bytes()
            rows = list(csv.reader(raw.decode("utf-8-sig").splitlines(), delimiter="\t"))
            sources[group] = {
                "file": filename,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "rows": len(rows),
            }
            if group not in ("low", "high"):
                sources[group]["values"] = rows
                continue
            for col in range(0, len(rows[0]), 2):
                if not rows[0][col].strip():
                    continue
                constant = float(rows[0][col])
                for rownum, row in enumerate(rows[2:], 3):
                    label = row[col].strip() if col < len(row) else ""
                    if not label:
                        continue
                    match = re.fullmatch(r"\[(BAS|ADV|EXP|MAS|ReM),\s*(SD|DX)\]\s*(.*)", label)
                    if not match:
                        raise ValueError(f"Unrecognized label {filename}:{rownum}:{col + 1}")
                    diff, kind, title = match.groups()
                    candidates = index.get(
                        (
                            normalized(title),
                            "dx" if kind == "DX" else "standard",
                            DIFFICULTIES[diff],
                        ),
                        [],
                    )
                    if len(candidates) != 1:
                        raise ValueError(
                            f"Unmatched/ambiguous chart {filename}:{rownum}:{col + 1}: {label}"
                        )
                    chart = candidates[0]
                    if abs(chart["constant"] - constant) > 1e-6:
                        raise ValueError(f"Constant changed: {chart['chart_key']}")
                    mark = row[col + 1].strip() if col + 1 < len(row) else ""
                    records.append(
                        (
                            chart["chart_key"],
                            group,
                            constant,
                            bool(mark),
                            mark if group == "high" else "",
                            label,
                            filename,
                            rownum,
                            col + 1,
                        )
                    )
        conn.executemany("INSERT INTO legacy VALUES(?,?,?,?,?,?,?,?,?)", records)
        report = {
            "imported_at": now(),
            "total": len(records),
            "legacy_complete": sum(r[3] for r in records),
            "groups": {
                g: {
                    "total": sum(r[1] == g for r in records),
                    "complete": sum(r[3] for r in records if r[1] == g),
                }
                for g in ("low", "high")
            },
            "note": "原表完成包含删除但删除前未完成；原表标记不等同已核实AP。",
            "sources": sources,
        }
        conn.execute(
            "INSERT INTO metadata VALUES('import',?)", (json.dumps(report, ensure_ascii=False),)
        )

    def meta(self, name):
        with self.db() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (name,)).fetchone()
            return json.loads(row[0]) if row else None

    def preferences(self):
        return self.meta("preferences")

    def save_preferences(self, value):
        with self.db() as conn:
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES ('preferences', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(value, allow_nan=False),),
            )
        return value

    def all_charts(self):
        catalog = self.catalog
        with self.db() as conn:
            # Record fields and their revisions must describe one SQLite snapshot.
            conn.execute("BEGIN")
            legacy = {r["chart_key"]: dict(r) for r in conn.execute("SELECT * FROM legacy")}
            manual = {r["chart_key"]: dict(r) for r in conn.execute("SELECT * FROM manual")}
            provider = {r["chart_key"]: dict(r) for r in conn.execute("SELECT * FROM provider")}
            revisions = dict(
                conn.execute(
                    "SELECT chart_key,MAX(id) FROM events WHERE kind IN ('manual','manual_reset') GROUP BY chart_key"
                )
            )
            row = conn.execute("SELECT value FROM metadata WHERE key='last_sync'").fetchone()
            last_sync = json.loads(row[0]) if row else None
        items = ChartList(
            cn_versions=catalog.cn_versions, cn_matrix_versions=catalog.cn_matrix_versions
        )
        for key, c in catalog.items():

            def lookup(records, chart_key=key):
                return records.get(chart_key, records.get(canonical_chart_key(chart_key)))

            old, user, remote = lookup(legacy), lookup(manual), lookup(provider)
            fields = (
                "id",
                "difficulty",
                "chart_key",
                "song_id",
                "lxns_song_id",
                "title",
                "display_title",
                "artist",
                "type",
                "difficulty_index",
                "difficulty_name",
                "level",
                "constant",
                "divingfish_id",
                "jacket_url",
                "jp_release_version",
                "jp_release_version_candidate",
                "jp_release_provider",
                "jp_release_date",
                "release_status",
                "release_source_scope",
                "type_release_version_candidate",
                "version_pending",
                "_search",
            )
            item = {name: c.get(name) for name in fields}
            item["id"] = c.get("id", int(canonical_chart_key(key).split(":")[0]))
            item["difficulty"] = c.get("difficulty", c["difficulty_index"])
            item["cn_version"] = c.get("cn_version", c.get("api_version"))
            item["_cn_version_group"] = catalog.cn_version_group(item["cn_version"])
            manual_ap = bool(user["ap"]) if user else None
            provider_ap = bool(remote and remote["fc"] in ("ap", "app"))
            is_ap = manual_ap if manual_ap is not None else provider_ap
            # Import completion remains observable until explicitly corrected; sync never deletes it.
            complete = bool(is_ap or (old and old["complete"])) if manual_ap is None else manual_ap
            achievement = user["achievement"] if user and user["achievement"] is not None else None
            warnings = []
            if manual_ap is True and not provider_ap:
                if last_sync:
                    warnings.append("已保存你的 AP 记录；水鱼尚未记录 AP，可能还未传分。")
                else:
                    warnings.append("已保存你的 AP 记录；尚未同步水鱼。")
            if manual_ap is False and provider_ap:
                warnings.append("本地已按你的修改标为未完成；水鱼仍有 AP 记录。")
            if old and old["complete"] and not is_ap and manual_ap is None:
                warnings.append("原表完成标记尚未核实为 AP（原表可能包含删曲豁免）。")
                if last_sync:
                    warnings.append("水鱼暂未记录 AP；保留原表完成状态，可稍后核对。")
            item.update(
                {
                    "group": old["group_name"] if old else "catalog",
                    "in_original_table": bool(old),
                    "requires_venue": requires_venue(c, old),
                    "legacy_complete": bool(old and old["complete"]),
                    "legacy_venue": old["venue"] if old else "",
                    "manual_ap": manual_ap,
                    "manual_venue": user["venue"] if user else "",
                    "venue": user["venue"] if user else old["venue"] if old else "",
                    "note": user["note"] if user else "",
                    "manual_date": user["achieved_date"] if user else None,
                    "manual_updated_at": user["updated_at"] if user else None,
                    "manual_revision": revisions.get(
                        key, revisions.get(canonical_chart_key(key), 0)
                    ),
                    "achievement": achievement,
                    "provider_achievement": remote["achievement"] if remote else None,
                    "provider_fc": remote["fc"] if remote else None,
                    "provider_synced_at": remote["synced_at"] if remote else None,
                    "is_ap": is_ap,
                    "is_complete": complete,
                    "status": "app"
                    if is_ap
                    and (
                        achievement == 101
                        or (manual_ap is None and remote and remote["fc"] == "app")
                    )
                    else "ap"
                    if is_ap
                    else "legacy"
                    if complete
                    else "incomplete",
                    "conflict": bool(
                        last_sync
                        and (
                            (manual_ap is not None and manual_ap != provider_ap)
                            or (manual_ap is None and old and old["complete"] and not provider_ap)
                        )
                    ),
                    "warnings": warnings,
                }
            )
            items.append(item)
        return items

    def chart(self, key):
        if getattr(self.catalog, "schema_version", 1) >= 2:
            key = canonical_chart_key(key)
        return next((c for c in self.all_charts() if c["chart_key"] == key), None)

    def save_manual(self, key, data):
        catalog = self.catalog
        if getattr(catalog, "schema_version", 1) >= 2:
            key = canonical_chart_key(key)
        if key not in catalog:
            raise KeyError(key)
        venue = data["venue"].strip()
        at = now()
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check_manual_revision(conn, key, data["expected_revision"])
            legacy = conn.execute(
                "SELECT group_name FROM legacy WHERE chart_key=?", (key,)
            ).fetchone()
            if data["ap"] and requires_venue(catalog[key], legacy) and not venue:
                raise ValueError("请填写完成 AP 的机厅名称。")
            conn.execute(
                "INSERT INTO manual VALUES(?,?,?,?,?,?,?) ON CONFLICT(chart_key) DO UPDATE SET ap=excluded.ap,venue=excluded.venue,achievement=excluded.achievement,note=excluded.note,achieved_date=excluded.achieved_date,updated_at=excluded.updated_at",
                (
                    key,
                    data["ap"],
                    venue,
                    data.get("achievement"),
                    data["note"].strip(),
                    data.get("date"),
                    at,
                ),
            )
            conn.execute(
                "INSERT INTO events(chart_key,kind,created_at,data_json) VALUES(?,?,?,?)",
                (key, "manual", at, json.dumps(data, ensure_ascii=False)),
            )
        return self.chart(key)

    @staticmethod
    def _check_manual_revision(conn, key, expected):
        actual = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM events WHERE chart_key=? AND kind IN ('manual','manual_reset')",
            (key,),
        ).fetchone()[0]
        if actual != expected:
            raise ManualConflictError()

    def clear_manual(self, key, expected_revision):
        if getattr(self.catalog, "schema_version", 1) >= 2:
            key = canonical_chart_key(key)
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check_manual_revision(conn, key, expected_revision)
            conn.execute("DELETE FROM manual WHERE chart_key=?", (key,))
            conn.execute(
                "INSERT INTO events(chart_key,kind,created_at,data_json) VALUES(?,?,?,?)",
                (key, "manual_reset", now(), "{}"),
            )
        return self.chart(key)

    def token(self):
        token = os.environ.get("DIVINGFISH_IMPORT_TOKEN", "").strip()
        if token:
            return token, "environment"
        with self._token_lock:
            if self.secret_path.exists():
                try:
                    value = json.loads(self.secret_path.read_text())
                    token = value.get("divingfish_import_token", "")
                    if not isinstance(token, str):
                        raise ValueError("invalid stored credential")
                except (OSError, ValueError, AttributeError):
                    return "", "invalid"
                if token:
                    return token, "local"
        return "", None

    def save_token(self, token):
        with self._token_lock:
            fd, name = tempfile.mkstemp(prefix=".secrets-", suffix=".tmp", dir=self.runtime)
            temp = Path(name)
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump({"divingfish_import_token": token.strip()}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                temp.replace(self.secret_path)
                self._token_generation += 1
            finally:
                temp.unlink(missing_ok=True)

    @contextmanager
    def sync_lease(self):
        if not self._sync_lock.acquire(blocking=False):
            raise SyncBusyError()
        try:
            with self._token_lock:
                token, _ = self.token()
                generation = self._token_generation
            yield token, generation
        finally:
            self._sync_lock.release()

    def publish_sync(self, payload, generation):
        with self._token_lock:
            if generation != self._token_generation:
                raise SyncTokenChangedError()
            return self.import_provider(payload)

    def readiness(self):
        checks = {"catalog": False, "personal": False}
        for name, path in (
            ("catalog", self.catalog_directory() / "catalog.sqlite"),
            ("personal", self.db_path),
        ):
            try:
                conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
                try:
                    if conn.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                        continue
                    if name == "catalog":
                        checks[name] = (
                            bool(self.catalog)
                            and conn.execute("SELECT COUNT(*) FROM charts").fetchone()[0] > 0
                        )
                    else:
                        metadata = dict(
                            conn.execute(
                                "SELECT key,value FROM metadata WHERE key IN ('import','preferences')"
                            )
                        )
                        checks[name] = all(
                            isinstance(json.loads(metadata[key]), dict)
                            for key in ("import", "preferences")
                        )
                        for table in ("manual", "legacy", "provider", "events"):
                            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                finally:
                    conn.close()
            except (OSError, sqlite3.Error, ValueError, KeyError):
                checks[name] = False
        return checks

    def import_provider(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError("水鱼返回的数据不含完整 records 列表。")
        catalog, waterfish_map = self._catalog_snapshot
        known = getattr(catalog, "source_charts", catalog)
        at, incoming, unmatched = now(), [], []
        for record in payload["records"]:
            try:
                ext = waterfish_map.get(str(record["song_id"]))
                key = (
                    (
                        f"{ext['song_id']}:{int(record['level_index'])}"
                        if getattr(catalog, "schema_version", 1) >= 2
                        else f"{ext['song_id']}:{ext['type']}:{int(record['level_index'])}"
                    )
                    if ext
                    else None
                )
                if key not in known:
                    unmatched.append(
                        {"song_id": record.get("song_id"), "level_index": record.get("level_index")}
                    )
                    continue
                if record.get("type") and record["type"].upper() != (
                    "DX" if ext["type"] == "dx" else "SD"
                ):
                    raise ValueError("chart type disagrees with song id")
                fc = str(record.get("fc", "")).lower()
                if fc not in ("", "fc", "fcp", "ap", "app"):
                    raise ValueError("unknown fc")
                value = record.get("achievements")
                value = float(value) if value is not None else None
                if value is not None and (not math.isfinite(value) or not 0 <= value <= 101):
                    raise ValueError("invalid achievement")
                incoming.append((key, value, fc, at, json.dumps(record, ensure_ascii=False)))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("水鱼成绩格式异常，未修改本地记录。") from exc
        if len({r[0] for r in incoming}) != len(incoming):
            raise ValueError("水鱼返回重复谱面，未修改本地记录。")
        receipt = {
            "ok": True,
            "records": len(payload["records"]),
            "matched": len(incoming),
            "unmatched": len(unmatched),
            "synced_at": at,
        }
        with self.db() as conn:
            # Full response is a snapshot; absent remote charts stay absent, local/legacy survives.
            conn.execute("DELETE FROM provider")
            conn.executemany("INSERT INTO provider VALUES(?,?,?,?,?)", incoming)
            conn.execute(
                "INSERT INTO metadata VALUES('last_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(receipt),),
            )
            conn.execute(
                "INSERT INTO events(kind,created_at,data_json) VALUES(?,?,?)",
                ("sync", at, json.dumps(receipt)),
            )
        receipt["conflicts"] = sum(c["conflict"] for c in self.all_charts())
        return receipt


def summarize(charts):
    total = len(charts)
    complete = sum(c["is_complete"] for c in charts)
    return {
        "total": total,
        "complete": complete,
        "ap": sum(c["is_ap"] for c in charts),
        "remaining": total - complete,
        "conflicts": sum(c["conflict"] for c in charts),
        "legacy_complete": sum(c["legacy_complete"] for c in charts),
    }
