from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ap_records import RecordConflictError
from .auth import (
    AuthConfig,
    AuthStore,
    LoginBodyLimitMiddleware,
    auth_router,
    local_probe,
    public_paths,
)
from .catalog_decisions import StaleDecisionError
from .catalog_updates import BusyError, CatalogUpdates
from .completion_exports import completion_export_router
from .jackets import IMMUTABLE_CACHE, REVALIDATE_CACHE, JacketPreviews, etag_matches
from .store import (
    ROOT,
    Store,
    SyncBusyError,
    SyncTokenChangedError,
    normalized,
    now,
    summarize,
)
from .venue_summary import summarize_venues

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
    "pending_record",
    "record_revision",
    "venue_pending",
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


class TokenInput(BaseModel):
    token: str = Field(max_length=2048)

    @field_validator("token")
    @classmethod
    def no_header_injection(cls, value):
        if any(c in value for c in "\r\n\x00"):
            raise ValueError("Token 不应包含换行")
        return value


class APRecordInput(BaseModel):
    model_config = {"extra": "forbid"}
    venue: str = Field(default="", max_length=200)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0, strict=True)


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
    auth_config = AuthConfig.from_environment()
    assets = Path(os.environ.get("MAIMAI_ASSETS_DIR", ROOT / "data/assets"))
    frontend = Path(os.environ.get("MAIMAI_FRONTEND_DIR", ROOT / "frontend/dist"))
    if os.environ.get("MAIMAI_ENV") == "production":
        if not assets.is_dir():
            raise RuntimeError("Production assets directory is missing; set MAIMAI_ASSETS_DIR.")
        if not (frontend / "index.html").is_file():
            raise RuntimeError(
                "Production frontend/index.html is missing; set MAIMAI_FRONTEND_DIR."
            )
    store = store or Store()
    updater = updater if updater is not None else CatalogUpdates(store)
    previews = JacketPreviews(
        store.runtime / "jacket-thumbnails",
        lambda: (store.catalog_directory(), store.catalog_root),
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            updater.close()
            store.close()

    app = FastAPI(title="Ashitemaru · maimai", version="0.1.0", lifespan=lifespan)
    auth = AuthStore(auth_config, store.runtime)
    public_frontend = public_paths(frontend)
    app.state.auth = auth
    app.include_router(auth_router(auth))
    app.state.store = store
    app.state.catalog_updates = updater
    app.state.jacket_previews = previews
    app.include_router(completion_export_router(store.runtime / "completion-exports"))
    allowed_hosts = ["127.0.0.1", "localhost", "::1", "testserver"]
    allowed_hosts += [
        h.strip() for h in os.environ.get("MAIMAI_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(LoginBodyLimitMiddleware)

    @app.exception_handler(RequestValidationError)
    async def safe_validation(request: Request, exc):
        if request.url.path == "/api/auth/login":
            return JSONResponse({"detail": "Master key 格式无效。"}, status_code=422)
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
            expected_origin = (request.url.scheme, request.headers.get("host"))
            try:
                origin_parts = urlsplit(origin or "")
            except ValueError:
                return JSONResponse({"detail": "请求来源无效"}, status_code=403)
            foreign_origin = (
                origin and (origin_parts.scheme, origin_parts.netloc) != expected_origin
            )
            if foreign_origin or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "仅接受本应用页面的修改请求"}, status_code=403)
        path = request.url.path
        public_auth = (request.method, path) in {
            ("GET", "/api/auth/session"),
            ("POST", "/api/auth/login"),
            ("POST", "/api/auth/logout"),
        }
        public_static = request.method in ("GET", "HEAD") and path in public_frontend
        if auth_config.required and not (public_auth or public_static or local_probe(request)):
            if not auth.authenticated(request):
                return JSONResponse(
                    {"detail": "请先使用 Master key 登录。"},
                    status_code=401,
                    headers={"Cache-Control": "no-store"},
                )
        response = await call_next(request)
        successful_read = request.method in ("GET", "HEAD") and response.status_code in (200, 304)
        hashed_bundle = path in public_frontend and re.fullmatch(
            r"/assets/[^/]+-[A-Za-z0-9_-]{8,}\.(?:js|css|woff2?|ttf|png|svg|webp)", path
        )
        artwork = path.startswith("/cover-assets/") and Path(path).suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".svg",
            ".woff",
            ".woff2",
            ".ttf",
            ".otf",
        }
        if successful_read and hashed_bundle:
            response.headers["Cache-Control"] = IMMUTABLE_CACHE
        elif successful_read and artwork:
            response.headers["Cache-Control"] = REVALIDATE_CACHE
        elif successful_read and path.startswith("/jackets/"):
            # Jacket routes set their policy only after source/revision validation.
            response.headers.setdefault("Cache-Control", "no-store")
        elif auth_config.required or path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def public_many(charts, fields=None):
        urls = {}
        result = []
        for chart in charts:
            original = chart.get("jacket_url")
            if original not in urls:
                urls[original] = previews.url(original)
            item = (
                {field: chart.get(field) for field in fields}
                if fields
                else {key: value for key, value in chart.items() if not key.startswith("_")}
            )
            item["thumbnail_url"] = urls[original]
            result.append(item)
        return result

    def public(c):
        return public_many([c])[0]

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
        return {"ok": True, "release_sha": os.environ.get("MAIMAI_RELEASE_SHA", "development")}

    @app.get("/api/ready")
    def ready():
        checks = store.readiness()
        ok = all(checks.values())
        return JSONResponse(
            {
                "ok": ok,
                "release_sha": os.environ.get("MAIMAI_RELEASE_SHA", "development"),
                "checks": checks,
            },
            status_code=200 if ok else 503,
        )

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
            "items": public_many(items[offset : offset + limit]),
            "total": len(items),
            "offset": offset,
            "limit": limit,
        }

    @app.get("/api/completion-table")
    def completion_table(filters: dict = Depends(chart_filters), sort: ChartSort = "constant_desc"):
        items = sort_charts(filtered(store.all_charts(), **filters), sort)
        return {
            "items": public_many(items, COMPLETION_FIELDS),
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
    @app.delete("/api/charts/{chart_key}/manual")
    def retired_manual(chart_key: str):
        raise HTTPException(
            410,
            {
                "code": "manual_retired",
                "message": "请刷新页面，改用机厅登记。AP 状态由水鱼核准。",
            },
        )

    @app.get("/api/ap-records")
    def ap_records():
        records = store.ap_records()
        return {**records, "items": public_many(records["items"])}

    def record_conflict(chart_key):
        return HTTPException(
            409,
            {
                "code": "record_conflict",
                "message": "记录已在其他页面更新，请重新载入后再保存。",
                "chart": public(store.chart(chart_key)) if store.chart(chart_key) else None,
            },
        )

    @app.put("/api/charts/{chart_key}/ap-record")
    def save_ap_record(chart_key: str, value: APRecordInput):
        try:
            item = store.save_ap_record(chart_key, value.model_dump())
        except KeyError as exc:
            raise HTTPException(404, "谱面不存在") from exc
        except RecordConflictError:
            raise record_conflict(chart_key) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"chart": public(item)}

    @app.delete("/api/charts/{chart_key}/ap-record")
    def cancel_ap_record(chart_key: str, expected_revision: int = Query(ge=0)):
        try:
            item = store.cancel_ap_record(chart_key, expected_revision)
        except KeyError as exc:
            raise HTTPException(404, "谱面不存在") from exc
        except RecordConflictError:
            raise record_conflict(chart_key) from None
        return {"chart": public(item) if item else None}

    @app.get("/api/venues")
    def venues():
        return store.venue_list()

    @app.get("/api/venues/summary")
    def venue_summary():
        with store.db() as conn:
            conn.execute("BEGIN")
            names = [row["name"] for row in conn.execute("SELECT name FROM venues")]
            pending_records = [dict(row) for row in conn.execute("SELECT venue FROM ap_pending")]
        return summarize_venues(store.all_charts(), names, pending_records)

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
        try:
            with store.sync_lease() as (token, generation):
                return sync_provider(token, generation)
        except SyncBusyError:
            raise HTTPException(
                409, {"code": "sync_busy", "message": "水鱼同步正在进行，请等待完成。"}
            ) from None
        except SyncTokenChangedError:
            raise HTTPException(
                409,
                {
                    "code": "token_changed",
                    "message": "同步期间 Token 已更改，请重新同步。本地记录已保留。",
                },
            ) from None

    def sync_provider(token, generation):
        if not token:
            raise HTTPException(
                409,
                {
                    "code": "not_configured",
                    "message": "请先在设置中填写水鱼 Import-Token，再手动同步。",
                },
            )
        eligible_records = store.pending_snapshot()
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
            result = store.publish_sync(response.json(), generation, eligible_records)
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

    @app.get("/jackets/thumbnails/{song_id}/{revision}.webp")
    def jacket_thumbnail(song_id: str, revision: str, request: Request):
        try:
            _, current = previews.source(song_id + ".png")
            if current != revision:
                raise FileNotFoundError(revision)
            etag = f'"{revision}"'
            headers = {"Cache-Control": IMMUTABLE_CACHE, "ETag": etag}
            if etag_matches(request.headers.get("if-none-match"), etag):
                return Response(status_code=304, headers=headers)
            return Response(
                previews.image(song_id, revision), media_type="image/webp", headers=headers
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, "曲绘预览不存在，请刷新曲库列表。") from exc

    @app.get("/jackets/{filename}")
    def jacket(filename: str, request: Request):
        try:
            path, revision = previews.source(filename)
        except FileNotFoundError as exc:
            raise HTTPException(404, "曲绘不存在") from exc
        etag = f'"{revision}-original"'
        headers = {"Cache-Control": REVALIDATE_CACHE, "ETag": etag}
        if etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return FileResponse(path, media_type="image/png", headers=headers)

    app.mount("/cover-assets", StaticFiles(directory=assets), name="cover-assets")
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app
