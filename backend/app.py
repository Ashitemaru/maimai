from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .catalog_decisions import StaleDecisionError
from .catalog_updates import BusyError, CatalogUpdates
from .completion_exports import completion_export_router
from .store import ROOT, Store, normalized, now, summarize

VERSION_ORDER = (
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
)

ChartSort = Literal[
    "constant_desc",
    "constant_asc",
    "achievement_desc",
    "achievement_asc",
    "ap_desc",
    "ap_asc",
    "title",
]
CNVersion = Annotated[int, Field(ge=10000)] | Literal["__unknown__"]
COMPLETION_FIELDS = (
    "id",
    "difficulty",
    "chart_key",
    "title",
    "display_title",
    "type",
    "difficulty_name",
    "level",
    "constant",
    "jacket_url",
    "is_complete",
    "is_ap",
    "status",
    "conflict",
    "achievement",
    "provider_achievement",
)


def sort_charts(items, sort: ChartSort):
    def stable_key(c):
        return (-c["constant"], int(c["song_id"]), c["type"], c["difficulty_index"], c["chart_key"])

    def achievement_key(c):
        value = c.get("achievement")
        if value is None:
            value = c.get("provider_achievement")
        return (
            value is None,
            (value if sort == "achievement_asc" else -value) if value is not None else 0,
            *stable_key(c),
        )

    if sort == "title":

        def key(c):
            return (c["title"], c["difficulty_index"], c["type"])
    elif sort in ("achievement_desc", "achievement_asc"):
        key = achievement_key
    elif sort in ("ap_desc", "ap_asc"):

        def key(c):
            return ((-1 if sort == "ap_desc" else 1) * int(c["is_ap"]), *stable_key(c))
    else:

        def key(c):
            return (
                (-1 if sort == "constant_desc" else 1) * c["constant"],
                int(c["song_id"]),
                c["type"],
                c["difficulty_index"],
                c["chart_key"],
            )

    return sorted(items, key=key)


def chart_version(chart):
    return chart.get("jp_release_version") or "__unknown__"


def level_order(level):
    return float(level.rstrip("+")) + (0.5 if "+" in level else 0)


def chart_filters(
    q: str = "",
    group: Literal["all", "low", "high", "catalog"] = "all",
    difficulty: int | None = Query(default=None, ge=0, le=4),
    type: Literal["standard", "dx"] | None = None,
    status: Literal["all", "complete", "incomplete", "ap", "legacy", "conflict"] = "all",
    constant_min: float | None = None,
    constant_max: float | None = None,
    level: str | None = None,
    venue: str | None = None,
    version: str | None = None,
    cn_version: CNVersion | None = Query(default=None),
):
    return {
        "q": q,
        "group": group,
        "difficulty": difficulty,
        "type": type,
        "status": status,
        "constant_min": constant_min,
        "constant_max": constant_max,
        "level": level,
        "venue": venue,
        "version": version,
        "cn_version": cn_version,
    }


class ManualInput(BaseModel):
    ap: bool = True
    venue: str = Field(default="", max_length=200)
    achievement: float | None = Field(default=None, ge=0, le=101, allow_inf_nan=False)
    note: str = Field(default="", max_length=2000)
    date: str | None = None

    @field_validator("date")
    @classmethod
    def date_valid(cls, value):
        if not value:
            return None
        date.fromisoformat(value)
        return value


class TokenInput(BaseModel):
    token: str = Field(max_length=2048)

    @field_validator("token")
    @classmethod
    def no_header_injection(cls, value):
        if any(c in value for c in "\r\n\x00"):
            raise ValueError("Token 不应包含换行")
        return value


class PreferencesInput(BaseModel):
    model_config = {"extra": "forbid"}
    default_constant_min: float = Field(
        ge=0, le=20, multiple_of=0.1, allow_inf_nan=False, strict=True
    )
    default_constant_max: float = Field(
        ge=0, le=20, multiple_of=0.1, allow_inf_nan=False, strict=True
    )

    @field_validator("default_constant_min", "default_constant_max")
    @classmethod
    def normalize_constant(cls, value):
        return round(value, 1)

    @model_validator(mode="after")
    def ordered_range(self):
        if self.default_constant_min > self.default_constant_max:
            raise ValueError("默认定数下限不能高于上限")
        return self


class CatalogDecisionInput(BaseModel):
    base_catalog_revision: str = Field(min_length=1, max_length=150)
    base_decision_revision: int = Field(ge=0)
    object_key: str = Field(min_length=1, max_length=150)
    kind: Literal["identity", "version"]
    action: Literal["select", "reject", "set", "keep", "revoke"]
    candidate_id: str | None = Field(default=None, max_length=500)
    version: str | None = Field(default=None, max_length=100)
    date: str | None = None
    note: str = Field(default="", max_length=2000)

    @field_validator("date")
    @classmethod
    def valid_date(cls, value):
        if not value:
            return None
        date.fromisoformat(value)
        return value


class CatalogRecordFieldChoice(BaseModel):
    model_config = {"extra": "forbid"}
    field_key: str = Field(min_length=1, max_length=100)
    option_id: str = Field(min_length=1, max_length=500)


class CatalogRecordChoicesInput(BaseModel):
    model_config = {"extra": "forbid"}
    id: int = Field(ge=1)
    difficulty: int = Field(ge=0, le=4)
    choices: list[CatalogRecordFieldChoice] = Field(min_length=1, max_length=256)
    note: str = Field(default="", max_length=2000)


class CatalogRecordDecisionInput(CatalogRecordChoicesInput):
    base_catalog_revision: str = Field(min_length=1, max_length=150)
    base_decision_revision: int = Field(ge=0)


class CatalogRecordBatchInput(BaseModel):
    model_config = {"extra": "forbid"}
    base_catalog_revision: str = Field(min_length=1, max_length=150)
    base_decision_revision: int = Field(ge=0)
    records: list[CatalogRecordChoicesInput] = Field(min_length=1, max_length=10000)


class CatalogRecordRevokeInput(BaseModel):
    model_config = {"extra": "forbid"}
    base_catalog_revision: str = Field(min_length=1, max_length=150)
    base_decision_revision: int = Field(ge=0)
    id: int = Field(ge=1)
    difficulty: int = Field(ge=0, le=4)
    field_keys: list[str] | None = Field(default=None, min_length=1, max_length=256)


def create_app(store=None, updater=None):
    store = store or Store()
    updater = updater if updater is not None else CatalogUpdates(store)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            updater.close()

    app = FastAPI(title="Ashitemaru · maimai", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.catalog_updates = updater
    app.include_router(completion_export_router(store.runtime / "completion-exports"))
    allowed_hosts = ["127.0.0.1", "localhost", "::1", "testserver"]
    allowed_hosts += [
        h.strip() for h in os.environ.get("MAIMAI_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.exception_handler(RequestValidationError)
    async def safe_validation(request: Request, exc):
        if request.url.path == "/api/settings/divingfish":
            # Pydantic's default error includes the rejected input; never echo a secret.
            return JSONResponse({"detail": "Token 格式无效，请检查输入。"}, status_code=422)
        if request.url.path == "/api/settings/preferences":
            # Rejected NaN/Infinity values cannot be echoed through a JSON response.
            return JSONResponse(
                {"detail": "默认定数须为 0–20 的有限数值，精确到 0.1，且下限不能高于上限。"},
                status_code=422,
            )
        return await request_validation_exception_handler(request, exc)

    @app.middleware("http")
    async def local_mutations(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "仅接受本应用页面的修改请求"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def public(c):
        return {key: value for key, value in c.items() if not key.startswith("_")}

    def filtered(
        items,
        *,
        q="",
        group="all",
        difficulty=None,
        type=None,
        status="all",
        constant_min=None,
        constant_max=None,
        level=None,
        venue=None,
        version=None,
        cn_version=None,
    ):
        needle = normalized(q)
        ext = store.waterfish_map.get(needle)

        def matches(c):
            if group not in ("all", "catalog") and c["group"] != group:
                return False
            if needle:
                numeric_match = needle == str(c["song_id"]) or needle == str(c["divingfish_id"])
                if ext and int(needle) >= 10000:
                    numeric_match = c["song_id"] == ext["song_id"] and c["type"] == ext["type"]
                if needle not in c["_search"] and not numeric_match:
                    return False
            if difficulty is not None and c["difficulty_index"] != difficulty:
                return False
            if type and c["type"] != type:
                return False
            if constant_min is not None and c["constant"] < constant_min:
                return False
            if constant_max is not None and c["constant"] > constant_max:
                return False
            if level and c["level"] != level:
                return False
            if venue and venue not in c["venue"]:
                return False
            if version and chart_version(c) != version:
                return False
            if (
                cn_version is not None
                and (c.get("_cn_version_group") or "__unknown__") != cn_version
            ):
                return False
            return {
                "all": True,
                "complete": c["is_complete"],
                "incomplete": not c["is_complete"],
                "ap": c["is_ap"],
                "legacy": c["status"] == "legacy",
                "conflict": c["conflict"],
            }[status]

        return [c for c in items if matches(c)]

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "catalog_charts": len(store.catalog),
            "imported_charts": store.meta("import")["total"],
        }

    @app.get("/api/catalog/update")
    def catalog_update_status():
        return updater.status()

    @app.get("/api/catalog/update/report")
    def catalog_update_report():
        directory = store.catalog_directory()

        def read_optional(name, default):
            path = directory / name
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default

        report = read_optional("build_report.json", {})
        return {
            "diff": read_optional("catalog_diff.json", {}),
            "issues": report.get("issues", []),
            "sources": read_optional("raw/sources.json", []),
            "source_policy": report.get("source_policy"),
        }

    def begin_catalog_operation(operation):
        try:
            return operation()
        except (BusyError, StaleDecisionError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/catalog/update", status_code=202)
    def update_catalog():
        return begin_catalog_operation(updater.start)

    @app.post("/api/catalog/remy/retry", status_code=202)
    def retry_remy():
        return begin_catalog_operation(updater.retry_remy)

    @app.post("/api/catalog/remy/recheck", status_code=202)
    def recheck_remy():
        return begin_catalog_operation(lambda: updater.retry_remy(recheck=True))

    @app.get("/api/catalog/review")
    def catalog_review():
        return updater.review()

    @app.get("/api/catalog/records/review")
    def catalog_record_review():
        return updater.record_review()

    @app.post("/api/catalog/records/decisions", status_code=202)
    def catalog_record_decide(value: CatalogRecordDecisionInput):
        return begin_catalog_operation(lambda: updater.decide_record(value.model_dump()))

    @app.post("/api/catalog/records/decisions/batch", status_code=202)
    def catalog_records_decide(value: CatalogRecordBatchInput):
        return begin_catalog_operation(lambda: updater.decide_records(value.model_dump()))

    @app.get("/api/catalog/records/decisions")
    def catalog_record_decisions():
        return updater.manual_records()

    @app.post("/api/catalog/records/decisions/revoke", status_code=202)
    def catalog_record_revoke(value: CatalogRecordRevokeInput):
        return begin_catalog_operation(lambda: updater.revoke_record(value.model_dump()))

    @app.post("/api/catalog/decisions", status_code=202)
    def catalog_decide(value: CatalogDecisionInput):
        return begin_catalog_operation(lambda: updater.decide(value.model_dump()))

    @app.post("/api/catalog/decisions/reapply", status_code=202)
    def reapply_catalog_decisions():
        return begin_catalog_operation(updater.reapply_decisions)

    @app.get("/api/catalog/decisions/history")
    def catalog_decision_history(
        object_key: str | None = None, limit: int = Query(default=100, ge=1, le=500)
    ):
        return {"items": updater.decisions.history(object_key, limit)}

    @app.post("/api/catalog/rollback", status_code=202)
    def rollback_catalog():
        return begin_catalog_operation(updater.rollback)

    @app.get("/api/catalog/data")
    def catalog_data():
        path = store.catalog_directory() / "catalog.json"
        if not path.is_file():
            raise HTTPException(404, "曲库数据不存在")
        return FileResponse(path, media_type="application/json")

    def selection_summary(items):
        result = summarize(items)
        result["levels"] = [
            {"level": level, **summarize([c for c in items if c["level"] == level])}
            for level in sorted({c["level"] for c in items}, key=level_order)
        ]
        result["constants"] = [
            {"constant": value, **summarize([c for c in items if c["constant"] == value])}
            for value in sorted({c["constant"] for c in items})
        ]
        result["sync"] = store.meta("last_sync")
        return result

    @app.get("/api/summary")
    def summary(filters: dict = Depends(chart_filters)):
        all_items = store.all_charts()
        items = filtered(all_items, **filters)
        result = selection_summary(items)
        result["groups"] = {
            g: summarize([c for c in all_items if c["group"] == g])
            for g in ("low", "high", "catalog")
        }
        imported = store.meta("import")
        result["import_info"] = {
            k: imported[k] for k in ("imported_at", "total", "legacy_complete", "groups", "note")
        }
        return result

    @app.get("/api/chart-filters")
    def filter_options(group: Literal["all", "low", "high", "catalog"] = "all"):
        all_items = store.all_charts()
        items = filtered(all_items, group=group)
        version_positions = {name: index for index, name in enumerate(VERSION_ORDER)}
        versions = sorted(
            {chart_version(c) for c in items},
            key=lambda name: (
                name == "__unknown__",
                version_positions.get(name, len(VERSION_ORDER)),
                name,
            ),
        )
        labels = {"DX": "でらっくす", "DX PLUS": "でらっくす PLUS", "__unknown__": "版本待核实"}
        return {
            "levels": sorted({c["level"] for c in items}, key=level_order),
            "constants": sorted({c["constant"] for c in items}),
            "versions": [
                {"value": version, "label": labels.get(version, version)} for version in versions
            ],
            "cn_versions": getattr(all_items, "cn_versions", []),
            "cn_matrix_versions": getattr(all_items, "cn_matrix_versions", []),
        }

    @app.get("/api/charts")
    def charts(
        filters: dict = Depends(chart_filters),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=6000),
        sort: ChartSort = "constant_desc",
    ):
        items = sort_charts(filtered(store.all_charts(), **filters), sort)
        return {
            "items": [public(c) for c in items[offset : offset + limit]],
            "total": len(items),
            "offset": offset,
            "limit": limit,
        }

    @app.get("/api/completion-table")
    def completion_table(filters: dict = Depends(chart_filters), sort: ChartSort = "constant_desc"):
        items = sort_charts(filtered(store.all_charts(), **filters), sort)
        return {
            "items": [{field: c.get(field) for field in COMPLETION_FIELDS} for c in items],
            "total": len(items),
            "summary": selection_summary(items),
            "generated_at": now(),
        }

    @app.get("/api/completion-matrix")
    def completion_matrix(
        difficulty: int | None = Query(default=None, ge=0, le=4),
        constant_min: float | None = Query(default=None, allow_inf_nan=False),
        constant_max: float | None = Query(default=None, allow_inf_nan=False),
        cn_version_min: int | None = Query(default=None, ge=10000),
        cn_version_max: int | None = Query(default=None, ge=10000),
    ):
        if constant_min is not None and constant_max is not None and constant_min > constant_max:
            raise HTTPException(422, "最低定数不能高于最高定数")
        if (
            cn_version_min is not None
            and cn_version_max is not None
            and cn_version_min > cn_version_max
        ):
            raise HTTPException(422, "起始国服版本不能晚于结束版本")
        all_items = store.all_charts()
        items = filtered(
            all_items, difficulty=difficulty, constant_min=constant_min, constant_max=constant_max
        )
        versions = [
            dict(version)
            for version in getattr(all_items, "cn_matrix_versions", [])
            if (cn_version_min is None or int(version["value"]) >= cn_version_min)
            and (cn_version_max is None or int(version["value"]) <= cn_version_max)
        ]
        # An unknown CN release cannot be placed inside a known release range.
        if (
            cn_version_min is None
            and cn_version_max is None
            and any(c.get("_cn_version_group") is None for c in items)
        ):
            versions.append({"value": "__unknown__", "label": "未知", "title": "国服版本待核实"})

        def empty_count():
            return {"complete": 0, "total": 0, "remaining": 0}

        totals = {version["value"]: empty_count() for version in versions}
        summary = empty_count()
        rows = {}
        for chart in items:
            version = str(chart.get("_cn_version_group") or "__unknown__")
            if version not in totals:
                continue
            constant = chart["constant"]
            if constant not in rows:
                rows[constant] = {
                    "constant": constant,
                    "cells": {key: empty_count() for key in totals},
                    "total": empty_count(),
                }
            row = rows[constant]
            complete = int(bool(chart["is_complete"]))
            for counts in (row["cells"][version], row["total"], totals[version], summary):
                counts["complete"] += complete
                counts["total"] += 1
                counts["remaining"] += 1 - complete
        return {
            "versions": versions,
            "rows": [rows[value] for value in sorted(rows, reverse=True)],
            "totals": totals,
            "summary": summary,
        }

    @app.get("/api/charts/{chart_key}")
    def chart(chart_key: str):
        item = store.chart(chart_key)
        if not item:
            raise HTTPException(404, "谱面不存在")
        return public(item)

    @app.put("/api/charts/{chart_key}/manual")
    def manual(chart_key: str, value: ManualInput):
        try:
            item = store.save_manual(chart_key, value.model_dump())
        except KeyError as exc:
            raise HTTPException(404, "谱面不存在") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"chart": public(item), "warnings": item["warnings"]}

    @app.delete("/api/charts/{chart_key}/manual")
    def clear_manual(chart_key: str):
        if chart_key not in store.catalog:
            raise HTTPException(404, "谱面不存在")
        return {"chart": public(store.clear_manual(chart_key))}

    @app.get("/api/venues")
    def venues():
        from collections import Counter

        counts = Counter(c["venue"] for c in store.all_charts() if c["venue"] and c["is_complete"])
        return {"items": [{"name": name, "count": count} for name, count in counts.most_common()]}

    @app.get("/api/settings/divingfish")
    def settings():
        token, source = store.token()
        return {"configured": bool(token), "source": source, "sync": store.meta("last_sync")}

    @app.get("/api/settings/preferences", response_model=PreferencesInput)
    def preferences():
        return store.preferences()

    @app.put("/api/settings/preferences", response_model=PreferencesInput)
    def save_preferences(value: PreferencesInput):
        return store.save_preferences(value.model_dump())

    @app.put("/api/settings/divingfish")
    def save_settings(value: TokenInput):
        store.save_token(value.token)
        return settings()

    @app.post("/api/sync/divingfish")
    def sync():
        token, _ = store.token()
        if not token:
            raise HTTPException(
                409,
                {
                    "code": "not_configured",
                    "message": "请先在设置中填写水鱼 Import-Token，再手动同步。",
                },
            )
        try:
            response = httpx.get(
                "https://www.diving-fish.com/api/maimaidxprober/player/records",
                headers={"Import-Token": token},
                timeout=30,
                follow_redirects=False,
            )
            if response.status_code != 200:
                raise HTTPException(
                    502,
                    {
                        "code": "provider_error",
                        "message": f"水鱼返回 HTTP {response.status_code}，请检查 Token 或稍后重试。本地记录已保留。",
                    },
                )
            result = store.import_provider(response.json())
        except httpx.RequestError as exc:
            raise HTTPException(
                502, {"code": "network_error", "message": "暂时无法访问水鱼，本地记录已保留。"}
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                502,
                {
                    "code": "invalid_response",
                    "message": str(exc)
                    if str(exc).startswith("水鱼")
                    else "水鱼返回格式异常，本地记录已保留。",
                },
            ) from exc
        return result

    @app.get("/jackets/{filename}")
    def jacket(filename: str):
        if not re.fullmatch(r"[0-9]+\.png", filename):
            raise HTTPException(404, "曲绘不存在")
        for directory in (store.catalog_directory(), store.catalog_root):
            path = directory / "jackets" / filename
            if path.is_file():
                return FileResponse(
                    path, media_type="image/png", headers={"Cache-Control": "no-cache"}
                )
        raise HTTPException(404, "曲绘不存在")

    app.mount("/cover-assets", StaticFiles(directory=ROOT / "data/assets"), name="cover-assets")
    frontend = ROOT / "frontend/dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


app = create_app()
