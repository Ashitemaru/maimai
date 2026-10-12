"""Server-side venue claims and immutable evidence from complete score imports."""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4


class RecordConflictError(Exception):
    """The venue claim changed since the client loaded it."""


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

    def save_ap_record(self, key, data):
        chart = self.chart(key)
        if chart is None:
            raise KeyError(key)
        key = chart["chart_key"]
        venue = " ".join(data["venue"].split()) if chart["requires_venue"] else ""
        if chart["requires_venue"] and not venue:
            raise ValueError("请选择或填写机厅。")
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
                if self._record_revision(conn, key) != data["expected_revision"]:
                    raise RecordConflictError()
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
                    (key, claim_id, venue, created_at, at, revision, "待同步核实", chart_json),
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

    def import_provider(self, payload, eligible_records=None):
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError("水鱼返回的数据不含完整 records 列表。")
        if eligible_records is None:
            eligible_records = self.pending_snapshot()
        catalog, waterfish_map = self._catalog_snapshot
        known = getattr(catalog, "source_charts", catalog)
        at, sync_id = timestamp(), str(uuid4())
        incoming, details, unmatched, seen = [], [], [], set()
        try:
            for record in payload["records"]:
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
            raise ValueError("水鱼成绩格式异常，未修改本地记录。") from exc
        receipt = {
            "ok": True,
            "records": len(payload["records"]),
            "matched": len(incoming),
            "unmatched": len(unmatched),
            "synced_at": at,
            "sync_id": sync_id,
            "confirmed": 0,
        }
        scores = {row[0]: row for row in incoming}
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
                        else "水鱼暂未显示 AP"
                        if score
                        else "水鱼暂无这张成绩"
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
                "INSERT INTO events(kind,created_at,data_json) VALUES(?,?,?)",
                ("sync", at, receipt_json),
            )
        return receipt
