"""Catalog is read-only; personal observations live in a separate SQLite database."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import threading
import unicodedata
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .ap_records import APRecords, initialize_records, pending_record, read_record_state
from .legacy_import import import_legacy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PREFERENCES = {"default_constant_min": 13.0, "default_constant_max": 13.9}
# Tables written by removed features; dropped idempotently at startup.
DEAD_TABLES = ("manual", "score_sync_sources", "score_uploads")

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


def normalized(value):
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


class ChartIndex(dict):
    def __init__(self, *args, source_charts=None, versions_cn=(), **kwargs):
        super().__init__(*args, **kwargs)
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
    """Chart rows, a chart_key index and filter metadata from one catalog snapshot.

    Cached lists and their rows are shared by every reader and must not be mutated.
    """

    def __init__(self, *args, cn_versions=(), cn_matrix_versions=()):
        super().__init__(*args)
        self.cn_versions = cn_versions
        self.cn_matrix_versions = cn_matrix_versions
        self.by_key = {}


CHART_FIELDS = (
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
    "cn_version",
    "_search",
)


def requires_venue(chart, legacy):
    return legacy["group_name"] == "high" if legacy else chart["constant"] >= 12.6


class Store(APRecords):
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
        self._token_generation = 0
        # One process serves the app; every personal write goes through db().
        self._boot = uuid.uuid4().hex[:12]
        self._revision_lock = threading.Lock()
        self._personal_revision = 0
        self._catalog_revision = 0
        self._build_lock = threading.Lock()
        self._chart_cache = None
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
        self.reload_catalog()
        if os.environ.get("MAIMAI_ENV") == "production" and not self.catalog:
            raise RuntimeError("Production catalog has no admitted charts.")
        self._initialize()

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
        # Publish both indexes together; readers retain their existing snapshot.
        with self._revision_lock:
            self._catalog_snapshot = tuple(loaded_tuple)
            self._catalog_revision += 1

    def reload_catalog(self):
        self.install_catalog(self.load_catalog(self.catalog_directory()))

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
            if conn.total_changes:
                self._personal_changed()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _personal_changed(self):
        with self._revision_lock:
            self._personal_revision += 1

    def version_token(self):
        """Changes whenever the catalog snapshot or personal data changes."""
        with self._revision_lock:
            return f"{self._boot}-{self._catalog_revision}-{self._personal_revision}"

    def close(self):
        self._chart_cache = None

    def _charts(self):
        # Serialize cold builds. A commit during a build leaves the result tagged with
        # the older revision, so the next read rebuilds.
        with self._build_lock:
            with self._revision_lock:
                catalog = self.catalog
                revision = (self._catalog_revision, self._personal_revision)
            cached = self._chart_cache
            if cached and cached[0] == revision:
                return cached[1]
            charts = self._assemble_charts(catalog=catalog)
            self._chart_cache = (revision, charts)
            return charts

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
            return self._load_chart_catalog(conn, Path(path), versions_cn)
        finally:
            conn.close()

    def _load_chart_catalog(self, conn, directory, versions_cn):
        source_charts, mapping = self._source_chart_index(directory)
        charts = ChartIndex(source_charts=source_charts, versions_cn=versions_cn)
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

    def _initialize(self):
        with self.db() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy(
              chart_key TEXT PRIMARY KEY, group_name TEXT NOT NULL, constant REAL NOT NULL,
              complete INTEGER NOT NULL, venue TEXT NOT NULL, label TEXT NOT NULL,
              source_file TEXT NOT NULL, source_row INTEGER NOT NULL, source_col INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS provider(
              chart_key TEXT PRIMARY KEY, achievement REAL, fc TEXT NOT NULL,
              synced_at TEXT NOT NULL, raw_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, chart_key TEXT, kind TEXT NOT NULL,
              created_at TEXT NOT NULL, data_json TEXT NOT NULL);
            """)
            for table in DEAD_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            initialize_records(conn)
            if not conn.execute("SELECT 1 FROM metadata WHERE key='import' ").fetchone():
                self._import(conn)
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('preferences', ?)",
                (json.dumps(DEFAULT_PREFERENCES),),
            )
        self.db_path.chmod(0o600)

    def _import(self, conn):
        catalog, waterfish_map = self._catalog_snapshot
        import_legacy(conn, self.import_dir, catalog, waterfish_map, normalized)

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

    def all_charts(self, connection=None):
        """Shared read-only rows; pass a connection to see its uncommitted writes."""
        if connection is not None:
            return self._assemble_charts(connection=connection)
        return self._charts()

    def _assemble_charts(self, connection=None, catalog=None):
        if catalog is None:
            catalog = self.catalog
        with self.db() if connection is None else nullcontext(connection) as conn:
            # Record fields and their revisions must describe one SQLite snapshot.
            if connection is None:
                conn.execute("BEGIN")
            legacy = {r["chart_key"]: dict(r) for r in conn.execute("SELECT * FROM legacy")}
            provider = {
                row["chart_key"]: dict(row)
                for row in conn.execute("SELECT chart_key,achievement,fc,synced_at FROM provider")
            }
            record_state = read_record_state(conn)
            synced = conn.execute("SELECT 1 FROM metadata WHERE key='last_sync'").fetchone()
        items = ChartList(
            cn_versions=catalog.cn_versions, cn_matrix_versions=catalog.cn_matrix_versions
        )
        for key, c in catalog.items():
            old, remote = legacy.get(key), provider.get(key)
            evidence = record_state["evidence"].get(key)
            confirmed = record_state["confirmed"].get(key)
            pending = record_state["pending"].get(key)
            record_revision = record_state["revisions"].get(key)
            details = record_state["details"].get(key)
            item = {name: c.get(name) for name in CHART_FIELDS}
            item["_cn_version_group"] = catalog.cn_version_group(item["cn_version"])
            provider_ap = bool(remote and remote["fc"] in ("ap", "app"))
            is_ap = bool(confirmed or evidence or provider_ap)
            # Import completion remains observable; sync never deletes it.
            complete = bool(is_ap or (old and old["complete"]))
            venue = confirmed["venue"] if confirmed else old["venue"] if old else ""
            needs_venue = requires_venue(c, old)
            app = (evidence and evidence["fc"] == "app") or (remote and remote["fc"] == "app")
            item.update(
                {
                    "group": old["group_name"] if old else "catalog",
                    "in_original_table": bool(old),
                    "requires_venue": needs_venue,
                    "legacy_complete": bool(old and old["complete"]),
                    "legacy_venue": old["venue"] if old else "",
                    "venue": venue,
                    "venue_pending": bool(is_ap and needs_venue and not venue),
                    "pending_record": pending_record(pending) if pending else None,
                    "record_revision": record_revision["revision"] if record_revision else 0,
                    "ap_confirmed_at": evidence["confirmed_at"] if evidence else None,
                    "venue_confirmed_at": confirmed["confirmed_at"] if confirmed else None,
                    "provider_achievement": remote["achievement"] if remote else None,
                    "provider_fc": remote["fc"] if remote else None,
                    "provider_synced_at": remote["synced_at"] if remote else None,
                    "provider_source": "divingfish" if remote else None,
                    "provider_fs": details["fs"] if details else None,
                    "provider_dx_score": details["dx_score"] if details else None,
                    "provider_rate": details["rate"] if details else None,
                    "provider_ra": details["ra"] if details else None,
                    "is_ap": is_ap,
                    "is_complete": complete,
                    "status": "app"
                    if is_ap and app
                    else "ap"
                    if is_ap
                    else "legacy"
                    if complete
                    else "incomplete",
                    "conflict": bool(
                        synced
                        and remote
                        and not provider_ap
                        and ((old and old["complete"]) or evidence)
                    ),
                }
            )
            items.append(item)
            items.by_key[key] = item
        return items

    def chart(self, key):
        return self._charts().by_key.get(key)

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
                self._personal_changed()
            finally:
                temp.unlink(missing_ok=True)

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
                        for table in ("legacy", "provider", "events"):
                            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                finally:
                    conn.close()
            except (OSError, sqlite3.Error, ValueError, KeyError):
                checks[name] = False
        return checks


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
