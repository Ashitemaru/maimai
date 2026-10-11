"""Publish complete catalog snapshots; Remy enrichment runs independently."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from scripts.remy_release import RemyFetcher
from scripts.remy_release import run as resolve_remy

from .catalog_decisions import CatalogDecisions, StaleDecisionError


class BusyError(Exception):
    pass


class Cancelled(Exception):
    pass


def now():
    return datetime.now(UTC).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def conflict_fingerprint(conflict):
    def stable(value):
        if isinstance(value, dict):
            return {
                k: stable(v)
                for k, v in value.items()
                if k not in {"evidence_file", "retrieved_at", "fetched_at", "checked_at", "current"}
            }
        if isinstance(value, list):
            return sorted(
                (stable(v) for v in value),
                key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False),
            )
        return value

    return json.dumps(stable(conflict), sort_keys=True, ensure_ascii=False)


def prepare(stage, previous, progress):
    from scripts.catalog_pipeline import prepare_update

    return prepare_update(stage, previous, progress)


def build(stage):
    from scripts.build_catalog import build as build_catalog

    return build_catalog(stage)


def validate(stage):
    from scripts.validate_catalog import validate as validate_catalog

    return validate_catalog(stage)


def clone(source, destination):
    """Reuse large immutable images; all writers must replace inherited files."""
    source, destination = Path(source), Path(destination)

    def ignore(directory, names):
        excluded = {"updates", "current", "remy-cache"} if Path(directory) == source else set()
        return [n for n in names if n in excluded or n.endswith((".tmp", ".part"))]

    def link(src, dst):
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
        return dst

    shutil.copytree(source, destination, copy_function=link, ignore=ignore)


def snapshot_at(directory):
    conn = sqlite3.connect(f"file:{directory / 'catalog.sqlite'}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value_json FROM metadata WHERE key='snapshot_at'").fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def public_diff(value):
    counts = value.get("counts", {})
    return {
        "songs_added": counts.get("added_songs", 0),
        "charts_added": counts.get("added_charts", 0),
        "constants_changed": counts.get("constant_changes", 0),
        "songs_missing": counts.get("missing_songs", 0),
        "charts_missing": counts.get("missing_charts", 0),
    }


def public_warnings(prepared, built):
    messages = [
        w if isinstance(w, str) else w.get("message", str(w)) for w in prepared.get("warnings", [])
    ]
    counts = built.get("counts", {})
    pending = counts.get("pending", counts.get("pending_charts", 0))
    if pending:
        messages.append(f"{pending} 张谱面待裁决，尚未入库")
    missing = prepared.get("diff", {}).get("counts", {}).get("missing_charts", 0)
    if missing:
        messages.append(f"落雪本次缺少 {missing} 张旧谱，原始记录和个人成绩已保留")
    return messages


class CancelableFetcher(RemyFetcher):
    def __init__(self, cache_dir, cancelled):
        self.cancelled = cancelled
        super().__init__(cache_dir, sleep=self.pause)

    def pause(self, seconds):
        if self.cancelled.wait(seconds):
            raise Cancelled()

    def fetch(self, url):
        if self.cancelled.is_set():
            raise Cancelled()
        result = super().fetch(url)
        if self.cancelled.is_set():
            raise Cancelled()
        return result


class CatalogUpdates:
    def __init__(
        self,
        store,
        *,
        prepare_fn=prepare,
        build_fn=build,
        validate_fn=validate,
        remy_fn=resolve_remy,
        fetcher_factory=CancelableFetcher,
    ):
        self.store = store
        self.root = store.catalog_root.resolve()
        self.workspace = self.root / "updates"
        self.snapshots = self.workspace / "snapshots"
        self.state_path = self.workspace / "status.json"
        self._prepare, self._build, self._validate = prepare_fn, build_fn, validate_fn
        self._remy, self._fetcher_factory = remy_fn, fetcher_factory
        self._lock = threading.RLock()
        self._main_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catalog-update")
        self._remy_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remy-check")
        self._closed = False
        self._epoch = 0
        self._cancel_remy = threading.Event()
        self._main_future = self._remy_future = None
        current = self.store.catalog_directory()
        decisions_path = current / "catalog_adjudications.json"
        self.decisions = CatalogDecisions(
            self.workspace / "adjudications.sqlite3",
            read(decisions_path) if decisions_path.exists() else None,
        )
        manifest = read(current / "snapshot.json") if (current / "snapshot.json").exists() else {}
        previous = read(self.state_path) if self.state_path.exists() else {}
        revision = manifest.get("id") or "initial-" + digest(current / "catalog.sqlite")[:12]
        self._state = {
            "revision": revision,
            "snapshot_at": snapshot_at(current),
            "previous_snapshot": manifest.get("parent"),
            "main": {"status": "idle", "stage": "idle", "message": "尚未更新曲库", "warnings": []},
            "remy": self._remy_summary(),
            "history": previous.get("history", [])[-30:],
        }
        if previous:
            self._state["main"] = previous["main"]
            if previous.get("revision") == revision:
                self._state["previous_snapshot"] = previous.get("previous_snapshot")
                self._state["remy"].update(previous.get("remy", {}))
            if self._state["main"]["status"] == "running":
                published = manifest.get("job_id") == self._state["main"].get("job_id")
                self._state["main"].update(
                    status="succeeded" if published else "failed",
                    stage="finished",
                    message="曲库已发布" if published else "上次更新已中断，当前曲库仍可使用",
                )
            if self._state["remy"]["status"] in {"queued", "running"}:
                self._state["remy"].update(status="pending", message="版本核准已中断，可稍后重试")

    def _remy_summary(self):
        review_path = self.store.catalog_directory() / "catalog_record_review.json"
        review = read(review_path) if review_path.exists() else {}
        if review.get("schema_version") == 2 and "remy_counts" in review:
            counts = review["remy_counts"]
            total, pending = counts["total"], counts["pending"]
        elif review.get("schema_version") == 2:
            # Older schema-2 reports may lack the summary; include unknown formal white charts.
            formal = {
                chart.get("chart_key"): chart
                for chart in self.store.catalog.values()
                if chart.get("difficulty", chart.get("difficulty_index")) == 4
            }
            records = [
                record for record in review.get("records", []) if record.get("difficulty") == 4
            ]
            total, pending = len(records), 0
            for record in records:
                chart = formal.get(record.get("chart_key"))
                required = any(
                    field.get("field_key") == "jp_release_version" and field.get("required", True)
                    for field in record.get("fields", [])
                )
                if (
                    required
                    or chart
                    and (not chart.get("jp_release_version") or chart.get("version_pending"))
                ):
                    pending += 1
        else:
            charts = [
                c
                for c in self.store.catalog.values()
                if c.get("difficulty", c.get("difficulty_index")) == 4
            ]
            total = len(charts)
            pending = sum(
                not c.get("jp_release_version")
                or c.get("jp_release_provider") not in {"remywiki", "manual"}
                for c in charts
            )
        return {
            "status": "pending" if pending else "complete",
            "pending_count": pending,
            "verified_count": total - pending,
            "updated_count": 0,
            "message": f"{pending} 张版本待核实" if pending else "版本已核准",
        }

    def _save(self):
        write(self.state_path, self._state)

    def _save_committed(self):
        try:
            self._save()
        except OSError:
            self._state["main"].setdefault("warnings", []).append(
                "曲库已发布，更新记录暂未写入磁盘"
            )

    def status(self):
        with self._lock:
            result = deepcopy(self._state)
            previous = result.pop("previous_snapshot", None)
            result["can_rollback"] = bool(
                previous and (self.snapshots / previous / "catalog.sqlite").is_file()
            )
            document = self.decisions.snapshot()
            path = self.store.catalog_directory() / "catalog_adjudications.json"
            applied = read(path).get("revision", 0) if path.exists() else 0
            result.update(
                decision_revision=document["revision"],
                applied_decision_revision=applied,
                decisions_pending=document["revision"] != applied,
            )
            return result

    def review(self):
        from scripts.build_catalog import JP_VERSION_BOUNDARIES

        with self._lock:
            current = self.store.catalog_directory()
            path = current / "catalog_review.json"
            report = read(path) if path.exists() else {"items": []}
            decisions = self.decisions.snapshot()
            active = {}
            for value in decisions["decisions"]:
                if value["kind"] in {"identity", "version"}:
                    active.setdefault(value["object_key"], []).append(value)
            for item in report["items"]:
                item["decisions"] = active.get(item["object_key"], [])
            present = {item["object_key"] for item in report["items"]}
            for key in sorted(active.keys() - present):
                parts = key.split(":")
                sid = int(parts[0]) if parts[0].isdigit() else 0
                kind = parts[1] if len(parts) > 1 else "unknown"
                ordinary = key.endswith(":ordinary")
                report["items"].append(
                    {
                        "object_key": key,
                        "scope": "ordinary_format" if ordinary else "chart",
                        "song_id": sid,
                        "type": kind,
                        "difficulty_index": None if ordinary else 4,
                        "title": f"ID {sid}（当前曲库无此谱面）",
                        "artist": "",
                        "object_fingerprint": active[key][0].get("object_fingerprint", ""),
                        "current": {
                            "version": None,
                            "date": None,
                            "provider": None,
                            "status": "unresolved",
                        },
                        "candidates": [],
                        "reasons": ["object_missing"],
                        "status": "pending",
                        "decisions": active[key],
                        "unavailable": True,
                    }
                )
            report.update(
                revision=self._state["revision"],
                decision_revision=decisions["revision"],
                decisions_pending=self.status()["decisions_pending"],
                versions=[name for _, name in JP_VERSION_BOUNDARIES],
                initialized=path.exists(),
            )
            return report

    def record_review(self, *, pending_only=True):
        from scripts.catalog_chart_compiler import load_record_review

        with self._lock:
            decisions = self.decisions.snapshot()
            report = load_record_review(self.store.catalog_directory(), adjudications=decisions)
            records = report.get("records", [])
            if "counts" not in report:
                pending = sum(record.get("status") == "pending" for record in records)
                report["counts"] = {
                    "total": len(records),
                    "pending": pending,
                    "admitted": len(records) - pending,
                }
            if pending_only:
                report["records"] = [
                    record for record in records if record.get("status") == "pending"
                ]
            report.update(
                revision=self._state["revision"],
                decision_revision=decisions["revision"],
                decisions_pending=self.status()["decisions_pending"],
            )
            return report

    def manual_records(self):
        with self._lock:
            report = self.record_review(pending_only=False)
            metadata = {(row["id"], row["difficulty"]): row for row in report.get("records", [])}
            records = {}
            for decision in self.decisions.snapshot()["decisions"]:
                if decision.get("kind") != "chart_field":
                    continue
                key = (decision["id"], decision["difficulty"])
                if key not in records:
                    source = metadata.get(key, {})
                    records[key] = {
                        "id": key[0],
                        "difficulty": key[1],
                        "chart_key": f"{key[0]}:{key[1]}",
                        "title": source.get("title", f"ID {key[0]}"),
                        "type": source.get("type"),
                        "difficulty_name": source.get("difficulty_name"),
                        "fields": [],
                        "unavailable": not bool(source),
                    }
                records[key]["fields"].append(deepcopy(decision))
            return {
                "revision": report["revision"],
                "decision_revision": report["decision_revision"],
                "decisions_pending": report["decisions_pending"],
                "records": [records[key] for key in sorted(records)],
            }

    def _decision_context(self, request):
        if request["base_catalog_revision"] != self._state["revision"]:
            raise StaleDecisionError("曲库已变化，请刷新后重试")
        document = self.decisions.snapshot()
        if request["base_decision_revision"] != document["revision"]:
            raise StaleDecisionError("裁决已变化，请刷新后重试")
        if any(d.get("kind") != "chart_field" for d in document["decisions"]):
            raise ValueError("旧裁决尚未完成谱面主键升级，请完成迁移后再提交")
        return document

    def _save_chart_decisions(self, values, expected_revision):
        if not values:
            raise ValueError("没有需要保存的字段")
        ident, lease, epoch = self._reserve_main("decisions")
        try:
            self.decisions.save_many(values, expected_revision)
            self._main_future = self._main_pool.submit(self._rebuild, ident, lease, epoch)
        except Exception as error:
            lease.close()
            self._failed(epoch, error)
            raise
        return self.status()

    def decide_record(self, request):
        return self.decide_records(
            {
                "base_catalog_revision": request["base_catalog_revision"],
                "base_decision_revision": request["base_decision_revision"],
                "records": [
                    {
                        key: value
                        for key, value in request.items()
                        if key not in {"base_catalog_revision", "base_decision_revision"}
                    }
                ],
            }
        )

    def decide_records(self, request):
        from scripts.catalog_chart_compiler import validate_chart_decisions

        with self._lock:
            document = self._decision_context(request)
            records = request.get("records")
            if not isinstance(records, list) or not 1 <= len(records) <= 10000:
                raise ValueError("请选择至少一张谱面，每次最多提交 10000 张")
            review = self.record_review(pending_only=False)
            available = {(row["id"], row["difficulty"]): row for row in review.get("records", [])}
            values, chart_keys, field_keys = [], set(), set()
            for item in records:
                if (
                    not isinstance(item, dict)
                    or type(item.get("id")) is not int
                    or type(item.get("difficulty")) is not int
                ):
                    raise ValueError("谱面主键格式无效")
                key = (item["id"], item["difficulty"])
                if key in chart_keys:
                    raise ValueError("同一次提交不能重复裁决同一谱面")
                chart_keys.add(key)
                record = available.get(key)
                if record and record.get("decision_pending"):
                    raise ValueError(
                        "该谱面的选择已保存，等待应用；请重新应用已保存裁决，无需重复提交"
                    )
                choices = validate_chart_decisions(
                    {"records": [record] if record else []},
                    item["id"],
                    item["difficulty"],
                    item.get("choices"),
                )
                expected_prefix = f"chart:{item['id']}:{item['difficulty']}:"
                for value in choices:
                    if (
                        value.get("kind") != "chart_field"
                        or value.get("id") != item["id"]
                        or value.get("difficulty") != item["difficulty"]
                    ):
                        raise ValueError("字段选择与谱面主键不匹配")
                    expected_key = expected_prefix + value.get("field_key", "")
                    if (
                        value.get("object_key") != expected_key
                        or not value.get("field_key")
                        or "value" not in value
                    ):
                        raise ValueError("谱面字段裁决无效")
                    if expected_key in field_keys:
                        raise ValueError("同一次提交不能重复裁决同一字段")
                    field_keys.add(expected_key)
                    value["note"] = item.get("note", "")
                values.extend(choices)
            return self._save_chart_decisions(values, document["revision"])

    def revoke_record(self, request):
        with self._lock:
            document = self._decision_context(request)
            active = [
                decision
                for decision in document["decisions"]
                if decision.get("id") == request["id"]
                and decision.get("difficulty") == request["difficulty"]
            ]
            field_keys = request.get("field_keys")
            if field_keys is not None:
                if not field_keys or len(set(field_keys)) != len(field_keys):
                    raise ValueError("请选择不同的已裁决字段")
                available = {decision["field_key"] for decision in active}
                if not set(field_keys).issubset(available):
                    raise ValueError("部分字段没有可撤销的裁决，未修改任何字段")
                active = [decision for decision in active if decision["field_key"] in field_keys]
            if not active:
                raise ValueError("该谱面没有可撤销的人工裁决")
            values = [
                {
                    "object_key": d["object_key"],
                    "kind": "chart_field",
                    "id": d["id"],
                    "difficulty": d["difficulty"],
                    "field_key": d["field_key"],
                    "action": "revoke",
                }
                for d in active
            ]
            return self._save_chart_decisions(values, document["revision"])

    def migrate_legacy_decisions(self):
        """Maintenance step, called explicitly after the user-data backup before a schema upgrade."""
        from scripts.catalog_chart_compiler import migrate_decisions

        with self._lock:
            if self._closed or self._state["main"]["status"] == "running":
                raise BusyError("请等待当前任务完成后再迁移")
            document = self.decisions.snapshot()
            if document.get("schema_version") == 2:
                return document
            lease = self._lease()
            try:
                migrated = migrate_decisions(document, self.store.catalog_directory())
                self._epoch += 1
                self._cancel_remy.set()
                self.decisions.migrate_document(migrated, document["revision"])
                self._state["remy"].update(
                    status="pending", message="裁决格式已升级，曲库重建后继续核准"
                )
            finally:
                lease.close()
            return self.decisions.snapshot()

    def decide(self, request):
        raise ValueError("旧的逐字段版本裁决接口已停用，请在谱面差异表中一次提交全部冲突字段")

    def reapply_decisions(self):
        with self._lock:
            ident, lease, epoch = self._reserve_main("decisions")
            self._main_future = self._main_pool.submit(self._rebuild, ident, lease, epoch)
            return self.status()

    def _lease(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        handle = (self.workspace / "publish.lock").open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise BusyError("已有曲库更新正在运行") from exc
        return handle

    def _reserve_main(self, action):
        if self._closed:
            raise BusyError("服务正在关闭")
        if self._state["main"]["status"] == "running":
            raise BusyError("曲库正在更新")
        lease = self._lease()
        old_state = deepcopy(self._state)
        self._epoch += 1
        self._cancel_remy.set()
        ident = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self._state["main"] = {
            "status": "running",
            "stage": action,
            "message": {"rollback": "正在回退曲库", "decisions": "正在应用裁决"}.get(
                action, "正在获取曲库"
            ),
            "job_id": ident,
            "started_at": now(),
            "warnings": [],
        }
        self._state["remy"].update(status="queued", message="曲库发布后自动核准")
        try:
            self._save()
        except OSError:
            self._state = old_state
            if self._state["remy"]["status"] in {"queued", "running"}:
                self._state["remy"].update(status="pending", message="版本核准已中断，可稍后重试")
            lease.close()
            raise
        return ident, lease, self._epoch

    def start(self):
        with self._lock:
            ident, lease, epoch = self._reserve_main("sources")
            self._main_future = self._main_pool.submit(self._update, ident, lease, epoch)
            return self.status()

    def rollback(self):
        with self._lock:
            previous = self._state.get("previous_snapshot")
            if not previous or not (self.snapshots / previous / "catalog.sqlite").is_file():
                raise ValueError("尚无可回退的曲库")
            ident, lease, epoch = self._reserve_main("rollback")
            self._main_future = self._main_pool.submit(
                self._rollback, ident, lease, epoch, previous
            )
            return self.status()

    def _progress(self, epoch, phase, detail=None):
        messages = {
            "sources": "正在获取曲库",
            "jackets": "正在补充曲绘",
            "build": "正在整理曲库",
            "validate": "正在校验",
            "publish": "正在发布",
        }
        with self._lock:
            if self._closed or epoch != self._epoch:
                raise Cancelled()
            self._state["main"].update(stage=phase, message=messages.get(phase, phase))
            if detail:
                self._state["main"]["progress"] = detail
            self._save()

    def _manifest(self, directory, ident, parent, kind, job_id):
        metadata = {
            "id": ident,
            "parent": parent,
            "kind": kind,
            "job_id": job_id,
            "created_at": now(),
            "files": {
                n: digest(directory / n)
                for n in (
                    "catalog.sqlite",
                    "catalog.json",
                    "catalog_adjudications.json",
                    "catalog_review.json",
                    "catalog_record_review.json",
                    "pending_charts.json",
                    "chart_key_map.json",
                )
                if (directory / n).exists()
            },
        }
        write(directory / "snapshot.json", metadata)
        return metadata

    def _baseline(self, previous):
        if previous.parent == self.snapshots:
            return previous.name
        ident = "baseline-" + uuid.uuid4().hex[:12]
        target = self.snapshots / ident
        self.snapshots.mkdir(parents=True, exist_ok=True)
        clone(previous, target)
        self._manifest(target, ident, None, "baseline", None)
        return ident

    def _new_stage(self, previous, ident):
        target = self.workspace / "staging" / ident
        target.parent.mkdir(parents=True, exist_ok=True)
        clone(previous, target)
        write(target / "previous_catalog.json", read(previous / "catalog.json"))
        write(target / "catalog_adjudications.json", self.decisions.snapshot())
        return target

    def _publish(self, target, loaded, previous_id, metadata):
        decisions_file = target / "catalog_adjudications.json"
        built_revision = read(decisions_file).get("revision", 0) if decisions_file.exists() else 0
        if built_revision != self.decisions.snapshot()["revision"]:
            raise StaleDecisionError("裁决已变化，旧构建未发布，请重新应用裁决")
        at = snapshot_at(target)
        old_target = self.store.catalog_directory()
        had_pointer = (self.root / "current").is_symlink()
        old_loaded = (self.store.catalog, self.store.waterfish_map)
        pointer = self.root / (".current-" + uuid.uuid4().hex[:8])
        try:
            pointer.symlink_to(target.relative_to(self.root), target_is_directory=True)
            os.replace(pointer, self.root / "current")
            try:
                self.store.install_catalog(loaded)
            except Exception:
                if had_pointer:
                    pointer.symlink_to(old_target.relative_to(self.root), target_is_directory=True)
                    os.replace(pointer, self.root / "current")
                else:
                    (self.root / "current").unlink()
                self.store.install_catalog(old_loaded)
                raise
        finally:
            pointer.unlink(missing_ok=True)
        self._state.update(revision=target.name, snapshot_at=at, previous_snapshot=previous_id)
        self._state["history"] = (self._state["history"] + [metadata])[-30:]

    def _update(self, ident, lease, epoch):
        succeeded = False
        try:
            previous = self.store.catalog_directory()
            parent = self._baseline(previous)
            stage = self._new_stage(previous, ident)
            result = self._prepare(
                stage, previous, lambda phase, detail=None: self._progress(epoch, phase, detail)
            )
            self._progress(epoch, "build")
            built = self._build(stage)
            self._progress(epoch, "validate")
            self._validate(stage)
            loaded = self.store.load_catalog(stage)
            self._progress(epoch, "publish")
            metadata = self._manifest(stage, ident, parent, "catalog", ident)
            target = self.snapshots / ident
            stage.rename(target)
            with self._lock:
                if self._closed or epoch != self._epoch:
                    raise Cancelled()
                self._publish(target, loaded, parent, metadata)
                self._state["main"].update(
                    status="succeeded",
                    stage="finished",
                    message="曲库已更新",
                    finished_at=now(),
                    diff=public_diff(result.get("diff", {})),
                    warnings=public_warnings(result, built),
                )
                self._state["remy"] = self._remy_summary()
                self._state["remy"].update(status="queued", message="正在准备版本核准")
                self._save_committed()
                succeeded = True
        except Exception as error:
            self._failed(epoch, error)
        finally:
            lease.close()
        if succeeded:
            with self._lock:
                if epoch == self._epoch and not self._closed:
                    self._queue_remy(epoch)

    def _failed(self, epoch, error):
        with self._lock:
            if epoch != self._epoch:
                return
            self._state["main"].update(
                status="failed",
                stage="finished",
                message="更新未完成，保留原曲库",
                error="任务已中断" if isinstance(error, Cancelled) else str(error),
                finished_at=now(),
            )
            self._state["remy"] = self._remy_summary()
            try:
                self._save()
            except OSError:
                pass

    def _rollback(self, ident, lease, epoch, previous_id):
        try:
            restored = self.snapshots / previous_id
            metadata = read(restored / "snapshot.json")
            for name, expected in metadata["files"].items():
                if digest(restored / name) != expected:
                    raise ValueError("旧快照校验失败，未回退")
            parent = self._baseline(self.store.catalog_directory())
            stage = self._new_stage(restored, ident)
            built = self._build(stage)
            self._validate(stage)
            loaded = self.store.load_catalog(stage)
            metadata = self._manifest(stage, ident, parent, "rollback", ident)
            metadata["restored_from"] = previous_id
            write(stage / "snapshot.json", metadata)
            target = self.snapshots / ident
            stage.rename(target)
            with self._lock:
                if self._closed or epoch != self._epoch:
                    raise Cancelled()
                self._publish(target, loaded, parent, metadata)
                self._state["main"].update(
                    status="succeeded",
                    stage="finished",
                    message="已回退到上版曲库",
                    finished_at=now(),
                    warnings=public_warnings({}, built),
                )
                self._state["remy"] = self._remy_summary()
                self._save_committed()
        except Exception as error:
            self._failed(epoch, error)
        finally:
            lease.close()

    def _rebuild(self, ident, lease, epoch):
        try:
            previous = self.store.catalog_directory()
            parent = self._baseline(previous)
            stage = self._new_stage(previous, ident)
            self._progress(epoch, "build")
            built = self._build(stage)
            self._progress(epoch, "validate")
            self._validate(stage)
            loaded = self.store.load_catalog(stage)
            metadata = self._manifest(stage, ident, parent, "decisions", ident)
            target = self.snapshots / ident
            stage.rename(target)
            with self._lock:
                if self._closed or epoch != self._epoch:
                    raise Cancelled()
                rollback_to = self._state.get("previous_snapshot") or parent
                self._publish(target, loaded, rollback_to, metadata)
                self._state["main"].update(
                    status="succeeded",
                    stage="finished",
                    message="裁决已应用",
                    finished_at=now(),
                    warnings=public_warnings({}, built),
                )
                self._state["remy"] = self._remy_summary()
                self._save_committed()
        except Exception as error:
            self._failed(epoch, error)
        finally:
            lease.close()

    def retry_remy(self, recheck=False):
        with self._lock:
            if self._closed or self._state["main"]["status"] == "running":
                raise BusyError("请等待曲库更新完成")
            if self._state["remy"]["status"] in {"queued", "running"}:
                raise BusyError("版本核准正在运行")
            self._epoch += 1
            self._cancel_remy.set()
            self._queue_remy(self._epoch, recheck=recheck)
            return self.status()

    def _queue_remy(self, epoch, recheck=False):
        self._cancel_remy = threading.Event()
        current = self.store.catalog_directory()
        self._state["remy"].update(status="queued", message="版本核准排队中")
        self._save_committed()
        self._remy_future = self._remy_pool.submit(
            self._check_remy, epoch, current, self._cancel_remy, recheck
        )

    def _check_remy(self, epoch, previous, cancelled, recheck=False):
        lease = None
        try:
            with self._lock:
                if epoch != self._epoch or cancelled.is_set():
                    return
                self._state["remy"].update(status="running", message="正在核准 Re:MASTER 版本")
                self._save()
            fetcher = self._fetcher_factory(self.root / "remy-cache", cancelled)
            kwargs = {"recheck": True} if recheck else {}
            report = self._remy(
                previous, cache_dir=self.root / "remy-cache", fetcher=fetcher, **kwargs
            )
            if cancelled.is_set():
                raise Cancelled()
            with self._lock:
                if epoch != self._epoch or self.store.catalog_directory() != previous:
                    return
                pending = len(report["pending"])
                self._state["remy"].update(
                    pending_count=pending,
                    verified_count=report["target_count"] - pending,
                    updated_count=report["resolved_count"],
                    checked_at=now(),
                    conflicts=len(report["conflicts"]),
                    fetch_count=report["fetch_count"],
                )
                write(self.workspace / "remy-reports" / (uuid.uuid4().hex + ".json"), report)
            conflicts_file = previous / "remy_conflicts.json"
            old_conflicts = (
                read(conflicts_file).get("conflicts", []) if conflicts_file.exists() else []
            )
            merged_conflicts = {conflict_fingerprint(c): c for c in old_conflicts}
            for conflict in report["conflicts"]:
                merged_conflicts.setdefault(conflict_fingerprint(conflict), conflict)
            conflicts = list(merged_conflicts.values())
            if report["additions"] or conflicts != old_conflicts:
                with self._lock:
                    if epoch != self._epoch or self.store.catalog_directory() != previous:
                        raise Cancelled()
                ident = "remy-" + uuid.uuid4().hex[:16]
                parent = self._baseline(previous)
                stage = self._new_stage(previous, ident)
                proposed = deepcopy(report["proposed_overrides"])
                conflicts = deepcopy(conflicts)
                records = list(proposed["overrides"])
                for conflict in conflicts:
                    records.extend(conflict.get("candidates", []))
                    if isinstance(conflict.get("current"), dict):
                        records.append(conflict["current"])
                for record in records:
                    filename = record.get("evidence_file")
                    if filename and Path(filename).is_absolute():
                        source = Path(filename)
                        sha = digest(source)
                        if sha != record.get("evidence_sha256"):
                            raise ValueError("Remy 证据缓存校验失败")
                        relative = f"remy-evidence/{sha}.html"
                        destination = stage / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if not destination.exists():
                            shutil.copy2(source, destination)
                        record["evidence_file"] = relative
                write(stage / "remy_release_overrides.json", proposed)
                write(stage / "remy_conflicts.json", {"conflicts": conflicts})
                self._build(stage)
                self._validate(stage)
                loaded = self.store.load_catalog(stage)
                metadata = self._manifest(stage, ident, parent, "remy", ident)
                target = self.snapshots / ident
                stage.rename(target)
                with self._lock:
                    if epoch != self._epoch or cancelled.is_set():
                        raise Cancelled()
                    # Main updates run throughout parsing/building; only the final
                    # pointer exchange takes the shared publication lease.
                    lease = self._lease()
                    # Metadata enrichment should not consume the user's previous CN version.
                    rollback_to = self._state.get("previous_snapshot") or parent
                    self._publish(target, loaded, rollback_to, metadata)
            with self._lock:
                if epoch != self._epoch:
                    return
                record_report = self.store.catalog_directory() / "catalog_record_review.json"
                version_report = self.store.catalog_directory() / "catalog_review.json"
                compiled_review = read(record_report) if record_report.exists() else {}
                remaining_conflicts = len(report["conflicts"])
                if compiled_review.get("schema_version") == 2:
                    # A durable user value resolves the field; raw disagreements remain evidence only.
                    remaining_conflicts = sum(
                        record.get("difficulty") == 4
                        and any(
                            field.get("field_key") == "jp_release_version"
                            and len(
                                {
                                    json.dumps(
                                        option.get("value"), ensure_ascii=False, sort_keys=True
                                    )
                                    for option in field.get("options", [])
                                }
                            )
                            > 1
                            for field in record.get("fields", [])
                        )
                        for record in compiled_review.get("records", [])
                    )
                elif version_report.exists():
                    remaining_conflicts = sum(
                        item.get("scope") == "chart" and item.get("status") == "conflict"
                        for item in read(version_report).get("items", [])
                    )
                if compiled_review or version_report.exists():
                    summary = self._remy_summary()
                    pending = summary["pending_count"]
                    self._state["remy"].update(
                        pending_count=pending, verified_count=summary["verified_count"]
                    )
                has_issue = bool(pending or remaining_conflicts)
                errors = report.get("fetch_errors", [])
                message = (
                    f"{pending} 张版本待核实"
                    if pending
                    else (
                        f"{remaining_conflicts} 张谱面证据有冲突"
                        if remaining_conflicts
                        else "版本已核准"
                    )
                )
                if errors and pending:
                    message += "，稍后可重试"
                incomplete_recheck = recheck and not report.get("recheck_complete", False)
                if incomplete_recheck:
                    message = "重核未完成，保留原核准值，可稍后继续"
                self._state["remy"].update(
                    status="pending" if has_issue else "complete",
                    message=message,
                    errors=errors,
                    conflicts=remaining_conflicts,
                )
                self._state["remy"].update(
                    recheck=recheck,
                    recheck_complete=report.get("recheck_complete") if recheck else None,
                    checked_pages=len(report.get("fetched_page_urls", [])),
                )
                if incomplete_recheck:
                    self._state["remy"]["status"] = "pending"
                self._save_committed()
        except Cancelled:
            pass
        except Exception as error:
            with self._lock:
                if epoch == self._epoch:
                    self._state["remy"].update(
                        status="pending" if isinstance(error, BusyError) else "failed",
                        message="版本核准暂未完成，曲库可正常使用",
                        error=str(error),
                    )
                    try:
                        self._save()
                    except OSError:
                        pass
        finally:
            if lease:
                lease.close()

    def close(self):
        with self._lock:
            self._closed = True
            self._cancel_remy.set()
        self._main_pool.shutdown(wait=False, cancel_futures=True)
        self._remy_pool.shutdown(wait=False, cancel_futures=True)
