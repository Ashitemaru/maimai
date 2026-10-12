"""Server-side venue claims and immutable evidence from complete score imports."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4


class RecordConflictError(Exception):
    """The venue claim changed since the client loaded it."""


class ScoreAccountError(ValueError):
    """A complete import belongs to a different already-bound game account."""


class ScoreSourceError(ValueError):
    """An older score provider may not replace the authoritative game snapshot."""


def timestamp():
    return datetime.now(UTC).isoformat()


def initialize_records(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ap_pending(
          chart_key TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, venue TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
          reason TEXT NOT NULL, chart_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS ap_record_revisions(
          chart_key TEXT PRIMARY KEY, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ap_record_requests(
          request_id TEXT PRIMARY KEY, chart_key TEXT NOT NULL, venue TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ap_confirmations(
          chart_key TEXT PRIMARY KEY, venue TEXT NOT NULL, recorded_at TEXT NOT NULL,
          confirmed_at TEXT NOT NULL, sync_id TEXT NOT NULL, claim_id TEXT NOT NULL,
          evidence_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ap_evidence(
          chart_key TEXT PRIMARY KEY, fc TEXT NOT NULL, achievement REAL,
          confirmed_at TEXT NOT NULL, sync_id TEXT NOT NULL, raw_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS provider_syncs(
          id TEXT PRIMARY KEY, synced_at TEXT NOT NULL, receipt_json TEXT NOT NULL,
          records_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS score_sync_sources(
          sync_id TEXT PRIMARY KEY, source TEXT NOT NULL, account_hash TEXT);
        CREATE TABLE IF NOT EXISTS provider_details(
          chart_key TEXT PRIMARY KEY, sync_id TEXT NOT NULL, fs TEXT,
          dx_score INTEGER, rate TEXT, ra REAL);
        CREATE TABLE IF NOT EXISTS provider_unmatched(
          source_key TEXT PRIMARY KEY, sync_id TEXT NOT NULL, raw_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS venues(
          name TEXT PRIMARY KEY, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL);
    """)
    if "chart_json" not in {row[1] for row in conn.execute("PRAGMA table_info(ap_pending)")}:
        conn.execute("ALTER TABLE ap_pending ADD COLUMN chart_json TEXT NOT NULL DEFAULT '{}'")
    conn.execute(
        "INSERT OR IGNORE INTO score_sync_sources(sync_id,source) "
        "SELECT id,'divingfish' FROM provider_syncs"
    )


def pending_record(row):
    return {
        name: row[name]
        for name in ("id", "venue", "created_at", "updated_at", "revision", "reason")
    }


def read_record_state(conn):
    return {
        name: {row["chart_key"]: dict(row) for row in conn.execute(f"SELECT {fields} FROM {table}")}
        for name, table, fields in (
            ("pending", "ap_pending", "chart_key,id,venue,created_at,updated_at,revision,reason"),
            ("confirmed", "ap_confirmations", "chart_key,venue,confirmed_at"),
            ("evidence", "ap_evidence", "chart_key,fc,confirmed_at"),
            ("revisions", "ap_record_revisions", "chart_key,revision"),
            ("details", "provider_details", "chart_key,fs,dx_score,rate,ra"),
        )
    }


def validate_score(record, *, ordinary):
    """Validate before starting a transaction; unknown/宴 rows remain auditable."""
    if not isinstance(record, dict):
        raise ValueError("invalid record")
    song_id, difficulty = record["song_id"], record["level_index"]
    if isinstance(song_id, bool) or not str(song_id).isdigit() or int(song_id) <= 0:
        raise ValueError("invalid song id")
    if isinstance(difficulty, bool) or not isinstance(difficulty, int) or not 0 <= difficulty <= 5:
        raise ValueError("invalid difficulty")
    fc = record.get("fc", "")
    if not isinstance(fc, str) or fc.lower() not in ("", "fc", "fcp", "ap", "app"):
        raise ValueError("unknown fc")
    achievement = record.get("achievements")
    if achievement is not None:
        if isinstance(achievement, bool):
            raise ValueError("invalid achievement")
        achievement = float(achievement)
        # 宴 charts can exceed 101%; do not apply the ordinary-chart ceiling to them.
        if not math.isfinite(achievement) or not 0 <= achievement <= (101 if ordinary else 1000):
            raise ValueError("invalid achievement")
    dx_score = record.get("dxScore")
    if dx_score is not None and (
        isinstance(dx_score, bool) or not isinstance(dx_score, int) or dx_score < 0
    ):
        raise ValueError("invalid DX score")
    ra = record.get("ra")
    if ra is not None and (
        isinstance(ra, bool) or not isinstance(ra, int | float) or not math.isfinite(ra) or ra < 0
    ):
        raise ValueError("invalid rating")
    for key in ("fs", "rate"):
        if record.get(key) is not None and (
            not isinstance(record[key], str) or len(record[key]) > 40
        ):
            raise ValueError(f"invalid {key}")
    return achievement, fc.lower(), json.dumps(record, ensure_ascii=False, allow_nan=False)


class APRecords:
    """Mixed into Store so personal writes use its connection and catalog snapshot."""

    @staticmethod
    def _record_revision(conn, key):
        row = conn.execute(
            "SELECT revision FROM ap_record_revisions WHERE chart_key=?", (key,)
        ).fetchone()
        return row[0] if row else 0

    @classmethod
    def _advance_record_revision(cls, conn, key):
        revision = cls._record_revision(conn, key) + 1
        conn.execute(
            "INSERT INTO ap_record_revisions VALUES(?,?) ON CONFLICT(chart_key) "
            "DO UPDATE SET revision=excluded.revision",
            (key, revision),
        )
        return revision

    @staticmethod
    def _recorded_venue(conn, key):
        from .store import canonical_chart_key

        keys = (key, canonical_chart_key(key), key)
        # Match is_complete + venue in the chart projection, including old keys.
        for table, completed in (
            ("ap_confirmations", "1"),
            ("manual", "ap"),
            ("legacy", "complete"),
        ):
            row = conn.execute(
                f"SELECT venue,{completed} AS completed FROM {table} WHERE chart_key IN (?,?) "
                "ORDER BY CASE WHEN chart_key=? THEN 0 ELSE 1 END LIMIT 1",
                keys,
            ).fetchone()
            if row is None:
                continue
            if row["completed"]:
                return row["venue"]
            if table == "legacy":
                ap = conn.execute(
                    "SELECT 1 FROM ap_evidence WHERE chart_key IN (?,?) "
                    "UNION ALL SELECT 1 FROM provider WHERE chart_key IN (?,?) "
                    "AND fc IN ('ap','app') LIMIT 1",
                    (*keys[:2], *keys[:2]),
                ).fetchone()
                return row["venue"] if ap else ""
            return ""
        return ""

    @staticmethod
    def _known_venue(conn, venue):
        return (
            conn.execute(
                "SELECT 1 FROM venues WHERE name=? "
                "UNION ALL SELECT 1 FROM legacy WHERE venue=? "
                "UNION ALL SELECT 1 FROM manual WHERE venue=? "
                "UNION ALL SELECT 1 FROM ap_confirmations WHERE venue=? LIMIT 1",
                (venue, venue, venue, venue),
            ).fetchone()
            is not None
        )

    def save_ap_record(self, key, data):
        chart = self.chart(key)
        if chart is None:
            raise KeyError(key)
        key = chart["chart_key"]
        venue = data["venue"].strip() if chart["requires_venue"] else ""
        if chart["requires_venue"] and not venue:
            raise ValueError("请选择已有机厅。")
        chart_json = json.dumps(
            {
                field: chart.get(field)
                for field in (
                    "chart_key",
                    "title",
                    "display_title",
                    "type",
                    "difficulty_name",
                    "constant",
                    "jacket_url",
                )
            },
            ensure_ascii=False,
        )
        at = timestamp()
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            request = conn.execute(
                "SELECT chart_key,venue FROM ap_record_requests WHERE request_id=?",
                (data["request_id"],),
            ).fetchone()
            if request:
                if (request["chart_key"], request["venue"]) != (key, venue):
                    raise ValueError("请求编号已使用，请重新提交。")
            else:
                if self._recorded_venue(conn, key):
                    raise ValueError("此谱面已记录机厅，不能修改或新增待核记录。")
                if self._record_revision(conn, key) != data["expected_revision"]:
                    raise RecordConflictError()
                if venue and not self._known_venue(conn, venue):
                    raise ValueError("请选择已有机厅；新机厅请先在我的机厅中添加。")
                previous = conn.execute(
                    "SELECT * FROM ap_pending WHERE chart_key=?", (key,)
                ).fetchone()
                revision = self._advance_record_revision(conn, key)
                claim_id = previous["id"] if previous else str(uuid4())
                created_at = previous["created_at"] if previous else at
                conn.execute(
                    "INSERT INTO ap_pending VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(chart_key) "
                    "DO UPDATE SET venue=excluded.venue,updated_at=excluded.updated_at,"
                    "revision=excluded.revision,reason=excluded.reason,chart_json=excluded.chart_json",
                    (key, claim_id, venue, created_at, at, revision, "等待华立核准", chart_json),
                )
                conn.execute(
                    "INSERT INTO ap_record_requests VALUES(?,?,?)",
                    (data["request_id"], key, venue),
                )
                if venue:
                    conn.execute(
                        "INSERT INTO venues VALUES(?,?,?) ON CONFLICT(name) "
                        "DO UPDATE SET last_used_at=excluded.last_used_at",
                        (venue, at, at),
                    )
                conn.execute(
                    "INSERT INTO events(chart_key,kind,created_at,data_json) VALUES(?,?,?,?)",
                    (
                        key,
                        "ap_pending",
                        at,
                        json.dumps(
                            {"id": claim_id, "venue": venue, "revision": revision},
                            ensure_ascii=False,
                        ),
                    ),
                )
        return self.chart(key)

    def cancel_ap_record(self, key, expected_revision):
        from .store import canonical_chart_key

        if getattr(self.catalog, "schema_version", 1) >= 2:
            key = canonical_chart_key(key)
        chart = self.chart(key)
        key = chart["chart_key"] if chart else key
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute("SELECT 1 FROM ap_pending WHERE chart_key=?", (key,)).fetchone()
            if chart is None and pending is None:
                raise KeyError(key)
            if self._record_revision(conn, key) != expected_revision:
                raise RecordConflictError()
            if pending:
                conn.execute("DELETE FROM ap_pending WHERE chart_key=?", (key,))
                self._advance_record_revision(conn, key)
                conn.execute(
                    "INSERT INTO events(chart_key,kind,created_at,data_json) VALUES(?,?,?,?)",
                    (key, "ap_pending_cancelled", timestamp(), "{}"),
                )
        return self.chart(key)

    def pending_snapshot(self):
        with self.db() as conn:
            return {
                row["chart_key"]: (row["id"], row["revision"])
                for row in conn.execute("SELECT chart_key,id,revision FROM ap_pending")
            }

    def ap_records(self):
        with self.db() as conn:
            rows = conn.execute(
                "SELECT * FROM ap_pending ORDER BY updated_at DESC,chart_key"
            ).fetchall()
        if not rows:
            return {"items": [], "count": 0}
        catalog = self.catalog
        fields = (
            "chart_key",
            "title",
            "type",
            "difficulty_name",
            "constant",
            "jacket_url",
            "record_revision",
        )
        items = []
        for row in rows:
            chart = catalog.get(row["chart_key"])
            item = (
                {key: chart.get(key) for key in fields}
                if chart
                else {
                    **json.loads(row["chart_json"]),
                    "chart_key": row["chart_key"],
                    "title": json.loads(row["chart_json"]).get("title", row["chart_key"]),
                    "record_revision": row["revision"],
                }
            )
            # Keep the revision paired with the pending row read in this query,
            # even if another request edits it while chart metadata is assembled.
            item["record_revision"] = row["revision"]
            item["pending_record"] = pending_record(row)
            items.append(item)
        return {"items": items, "count": len(items)}

    def venue_list(self):
        counts = Counter(
            chart["venue"] for chart in self.all_charts() if chart["venue"] and chart["is_complete"]
        )
        with self.db() as conn:
            recent = {
                row["name"]: row["last_used_at"] for row in conn.execute("SELECT * FROM venues")
            }
        names = counts.keys() | recent.keys()
        return {
            "items": [
                {"name": name, "count": counts[name], "last_used_at": recent.get(name)}
                for name in sorted(
                    names, key=lambda name: (recent.get(name, ""), counts[name], name), reverse=True
                )
            ]
        }

    def import_provider(
        self, payload, eligible_records=None, *, source="divingfish", account_id=None, outcome=None
    ):
        if source not in ("divingfish", "wahlap"):
            raise ValueError("不支持的成绩来源。")
        label = "华立" if source == "wahlap" else "水鱼"
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError(f"{label}返回的数据不含完整 records 列表。")
        account_hash = None
        if source == "wahlap":
            if (
                not isinstance(account_id, str)
                or not account_id.isascii()
                or not account_id.isdigit()
                or not 0 < len(account_id) <= 20
                or int(account_id) <= 0
            ):
                raise ScoreAccountError("华立账号信息不完整，未修改本地记录。")
            account_hash = hashlib.sha256(f"wahlap:{int(account_id)}".encode()).hexdigest()
            if not 0 < len(payload["records"]) <= 50000:
                raise ValueError("华立未返回有效的完整成绩，未修改本地记录。")
        if eligible_records is None:
            eligible_records = self.pending_snapshot()
        catalog, waterfish_map = self._catalog_snapshot
        known = getattr(catalog, "source_charts", catalog)
        at, sync_id = timestamp(), str(uuid4())
        incoming, details, unmatched, seen = [], [], [], set()
        try:
            for record in payload["records"]:
                if source == "wahlap":
                    required = {"song_id", "level_index", "achievements", "fc", "fs", "dxScore"}
                    if not isinstance(record, dict) or set(record) != required:
                        raise ValueError("incomplete game score")
                    if (
                        isinstance(record["achievements"], bool)
                        or not isinstance(record["achievements"], int | float)
                        or record["fs"] not in ("", "sync", "fs", "fsp", "fsd", "fsdp")
                        or isinstance(record["dxScore"], bool)
                        or not isinstance(record["dxScore"], int)
                    ):
                        raise ValueError("invalid game score")
                if source == "wahlap":
                    music_id = int(record["song_id"])
                    ext = {
                        "song_id": music_id
                        if getattr(catalog, "schema_version", 1) >= 2
                        else music_id - 10000
                        if music_id >= 10000
                        else music_id,
                        "type": "dx" if music_id >= 10000 else "standard",
                    }
                else:
                    ext = waterfish_map.get(str(record["song_id"]))
                key = None
                if ext:
                    key = (
                        f"{ext['song_id']}:{record['level_index']}"
                        if getattr(catalog, "schema_version", 1) >= 2
                        else f"{ext['song_id']}:{ext['type']}:{record['level_index']}"
                    )
                ordinary = key in known
                achievement, fc, raw = validate_score(record, ordinary=ordinary)
                source_key = f"{int(record['song_id'])}:{record['level_index']}"
                if source_key in seen:
                    raise ValueError("duplicate record")
                seen.add(source_key)
                if not ordinary:
                    unmatched.append((source_key, sync_id, raw))
                    continue
                if record.get("type") and record["type"].upper() != (
                    "DX" if ext["type"] == "dx" else "SD"
                ):
                    raise ValueError("chart type disagrees with song id")
                incoming.append((key, achievement, fc, at, raw))
                details.append(
                    (
                        key,
                        sync_id,
                        record.get("fs"),
                        record.get("dxScore"),
                        record.get("rate"),
                        record.get("ra"),
                    )
                )
            if len({row[0] for row in incoming}) != len(incoming):
                raise ValueError("duplicate mapped chart")
            raw_records = json.dumps(payload["records"], ensure_ascii=False, allow_nan=False)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"{label}成绩格式异常，未修改本地记录。") from exc
        receipt = {
            "ok": True,
            "records": len(payload["records"]),
            "matched": len(incoming),
            "unmatched": len(unmatched),
            "synced_at": at,
            "sync_id": sync_id,
            "confirmed": 0,
            "source": source,
        }
        if source == "wahlap":
            receipt.update(
                {
                    key: value
                    for key, value in (outcome or {}).items()
                    if key in ("logout_confirmed", "logout_status", "notice")
                }
            )
        scores = {row[0]: row for row in incoming}
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            binding = conn.execute(
                "SELECT value FROM metadata WHERE key='wahlap_account_hash'"
            ).fetchone()
            if source == "divingfish" and binding:
                raise ScoreSourceError("已启用华立直连，请扫描二维码更新成绩。")
            if source == "wahlap":
                if binding and json.loads(binding[0]) != account_hash:
                    raise ScoreAccountError("二维码对应其他游戏账号，未修改本地记录。")
                conn.execute(
                    "INSERT OR IGNORE INTO metadata(key,value) VALUES('wahlap_account_hash',?)",
                    (json.dumps(account_hash),),
                )
            conn.execute(
                "INSERT INTO score_sync_sources(sync_id,source,account_hash) VALUES(?,?,?)",
                (sync_id, source, account_hash),
            )
            conn.execute("DELETE FROM provider")
            conn.executemany("INSERT INTO provider VALUES(?,?,?,?,?)", incoming)
            conn.execute("DELETE FROM provider_details")
            conn.executemany("INSERT INTO provider_details VALUES(?,?,?,?,?,?)", details)
            conn.execute("DELETE FROM provider_unmatched")
            conn.executemany("INSERT INTO provider_unmatched VALUES(?,?,?)", unmatched)
            for key, achievement, fc, _, raw in incoming:
                if fc not in ("ap", "app"):
                    continue
                # A later missing/non-AP response cannot revoke an observed AP.
                conn.execute(
                    "INSERT INTO ap_evidence VALUES(?,?,?,?,?,?) ON CONFLICT(chart_key) DO UPDATE SET "
                    "fc=excluded.fc,achievement=excluded.achievement,confirmed_at=excluded.confirmed_at,"
                    "sync_id=excluded.sync_id,raw_json=excluded.raw_json WHERE ap_evidence.fc!='app' OR excluded.fc='app'",
                    (key, fc, achievement, at, sync_id, raw),
                )
            for row in conn.execute("SELECT * FROM ap_pending").fetchall():
                key = row["chart_key"]
                if eligible_records.get(key) != (row["id"], row["revision"]):
                    continue
                if self._recorded_venue(conn, key):
                    conn.execute(
                        "UPDATE ap_pending SET reason=? WHERE chart_key=?",
                        ("已有机厅记录，待核记录未生效", key),
                    )
                    continue
                score = scores.get(key)
                if score and score[2] in ("ap", "app") and key in catalog:
                    conn.execute(
                        "INSERT INTO ap_confirmations VALUES(?,?,?,?,?,?,?) ON CONFLICT(chart_key) DO UPDATE SET "
                        "venue=excluded.venue,recorded_at=excluded.recorded_at,confirmed_at=excluded.confirmed_at,"
                        "sync_id=excluded.sync_id,claim_id=excluded.claim_id,evidence_json=excluded.evidence_json",
                        (key, row["venue"], row["created_at"], at, sync_id, row["id"], score[4]),
                    )
                    conn.execute("DELETE FROM ap_pending WHERE chart_key=?", (key,))
                    revision = self._advance_record_revision(conn, key)
                    conn.execute(
                        "INSERT INTO events(chart_key,kind,created_at,data_json) VALUES(?,?,?,?)",
                        (
                            key,
                            "ap_confirmed",
                            at,
                            json.dumps(
                                {
                                    "claim_id": row["id"],
                                    "venue": row["venue"],
                                    "sync_id": sync_id,
                                    "revision": revision,
                                    "source": source,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    receipt["confirmed"] += 1
                else:
                    reason = (
                        "谱面尚未匹配"
                        if key not in catalog
                        else f"{label}暂未显示 AP"
                        if score
                        else f"{label}暂无这张成绩"
                    )
                    conn.execute("UPDATE ap_pending SET reason=? WHERE chart_key=?", (reason, key))
            receipt["pending"] = conn.execute("SELECT COUNT(*) FROM ap_pending").fetchone()[0]
            conn.execute(
                "INSERT INTO metadata VALUES('last_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(receipt),),
            )
            charts = self.all_charts(connection=conn)
            receipt["conflicts"] = sum(chart["conflict"] for chart in charts)
            receipt["venue_pending"] = sum(chart["venue_pending"] for chart in charts)
            receipt_json = json.dumps(receipt)
            conn.execute(
                "INSERT INTO provider_syncs VALUES(?,?,?,?)",
                (sync_id, at, receipt_json, raw_records),
            )
            conn.execute(
                "INSERT INTO metadata VALUES('last_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (receipt_json,),
            )
            conn.execute(
                "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"last_{source}_sync", receipt_json),
            )
            conn.execute(
                "INSERT INTO events(kind,created_at,data_json) VALUES(?,?,?)",
                ("sync", at, receipt_json),
            )
        return receipt
