"""Durable catalog decisions, independent of personal scores and public snapshots."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class StaleDecisionError(Exception):
    pass


class CatalogDecisions:
    def __init__(self, path, bootstrap=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions(
                    object_key TEXT NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL,
                    PRIMARY KEY(object_key,kind));
                CREATE TABLE IF NOT EXISTS events(
                    revision INTEGER PRIMARY KEY, object_key TEXT NOT NULL, kind TEXT NOT NULL,
                    action TEXT NOT NULL, created_at TEXT NOT NULL, data_json TEXT NOT NULL);
            """)
            if conn.execute("SELECT value FROM metadata WHERE key='revision'").fetchone() is None:
                document = bootstrap or {"revision": 0, "decisions": []}
                conn.execute(
                    "INSERT INTO metadata VALUES ('revision',?)", (document.get("revision", 0),)
                )
                if document.get("schema_version", 1) >= 2:
                    conn.execute(
                        "INSERT INTO metadata VALUES ('schema_version',?)",
                        (document["schema_version"],),
                    )
                for item in document.get("decisions", []):
                    conn.execute(
                        "INSERT INTO decisions VALUES (?,?,?)",
                        (item["object_key"], item["kind"], json.dumps(item, ensure_ascii=False)),
                    )
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=20)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def snapshot(self):
        with self.db() as conn:
            conn.execute("BEGIN")
            revision = conn.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]
            decisions = [
                json.loads(row[0])
                for row in conn.execute("SELECT data_json FROM decisions ORDER BY object_key,kind")
            ]
            schema = conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
        document = {"revision": revision, "decisions": decisions}
        if schema:
            document["schema_version"] = schema[0]
        return document

    def migrate_document(self, document, expected_revision):
        """Explicit, atomic migration; preserve the complete previous decision set in audit history."""
        if document.get("schema_version") != 2 or document.get("revision") != expected_revision:
            raise ValueError("新版裁决迁移文档无效")
        values = [dict(value) for value in document.get("decisions", [])]
        keys = [(v.get("object_key"), v.get("kind")) for v in values]
        if len(keys) != len(set(keys)) or any(
            v.get("kind") != "chart_field"
            or "value" not in v
            or not v.get("object_key", "").startswith("chart:")
            for v in values
        ):
            raise ValueError("迁移包含重复或未转换的裁决，原数据未修改")
        timestamp = datetime.now(UTC).isoformat()
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revision = conn.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]
            if revision != expected_revision:
                raise StaleDecisionError("裁决已变化，请重新执行迁移")
            schema = conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if schema and schema[0] == 2:
                existing = [
                    json.loads(row[0])
                    for row in conn.execute(
                        "SELECT data_json FROM decisions ORDER BY object_key,kind"
                    )
                ]
                if existing != sorted(values, key=lambda v: (v["object_key"], v["kind"])):
                    raise ValueError("新版裁决不可重复转换")
                return revision
            previous = [
                json.loads(row[0])
                for row in conn.execute("SELECT data_json FROM decisions ORDER BY object_key,kind")
            ]
            revision += 1
            conn.execute("DELETE FROM decisions")
            for value in values:
                value["migration_revision"] = revision
                conn.execute(
                    "INSERT INTO decisions VALUES (?,?,?)",
                    (value["object_key"], value["kind"], json.dumps(value, ensure_ascii=False)),
                )
            event = {
                "decision": {
                    "object_key": "__catalog__",
                    "kind": "migration",
                    "action": "migrate",
                    "schema_version": 2,
                    "decision_revision": revision,
                    "migration": document.get("migration", {}),
                    "converted_count": len(values),
                },
                "previous": {"schema_version": schema[0] if schema else 1, "decisions": previous},
            }
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (
                    revision,
                    "__catalog__",
                    "migration",
                    "migrate",
                    timestamp,
                    json.dumps(event, ensure_ascii=False),
                ),
            )
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_version',2)")
            conn.execute("UPDATE metadata SET value=? WHERE key='revision'", (revision,))
        return revision

    def save(self, decision, expected_revision):
        return self.save_many([decision], expected_revision)

    def save_many(self, decisions, expected_revision):
        """Store all submitted chart choices atomically, with one durable audit event per field."""
        values = [dict(decision) for decision in decisions]
        if not values:
            raise ValueError("请选择至少一个字段")
        keys = [(value.get("object_key"), value.get("kind")) for value in values]
        if any(not all(key) for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("同一次提交不能重复裁决同一字段")
        timestamp = datetime.now(UTC).isoformat()
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revision = conn.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]
            if revision != expected_revision:
                raise StaleDecisionError("裁决已变化，请刷新后重试")
            batch_id = revision + 1
            # Check all revocations before changing anything; the transaction also rolls back
            # every earlier write if a later constraint or persistence error is raised.
            previous_values = {}
            for value, key in zip(values, keys, strict=False):
                previous = conn.execute(
                    "SELECT data_json FROM decisions WHERE object_key=? AND kind=?", key
                ).fetchone()
                if value["action"] == "revoke" and previous is None:
                    raise ValueError("没有可撤销的裁决")
                previous_values[key] = json.loads(previous[0]) if previous else None
            for value, key in zip(values, keys, strict=False):
                revision += 1
                value.update(updated_at=timestamp, decision_revision=revision, batch_id=batch_id)
                if value["action"] == "revoke":
                    conn.execute("DELETE FROM decisions WHERE object_key=? AND kind=?", key)
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO decisions VALUES (?,?,?)",
                        (*key, json.dumps(value, ensure_ascii=False)),
                    )
                event = {"decision": value, "previous": previous_values[key], "batch_id": batch_id}
                conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?)",
                    (
                        revision,
                        *key,
                        value["action"],
                        timestamp,
                        json.dumps(event, ensure_ascii=False),
                    ),
                )
            conn.execute("UPDATE metadata SET value=? WHERE key='revision'", (revision,))
        return revision

    def history(self, object_key=None, limit=100):
        with self.db() as conn:
            where, params = (" WHERE object_key=?", [object_key]) if object_key else ("", [])
            rows = conn.execute(
                "SELECT revision,created_at,data_json FROM events"
                + where
                + " ORDER BY revision DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [{"revision": row[0], "created_at": row[1], **json.loads(row[2])} for row in rows]
