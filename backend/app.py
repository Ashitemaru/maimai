from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

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
from .bilibili import BilibiliService, bilibili_router
from .catalog_decisions import StaleDecisionError
from .catalog_updates import BusyError, CatalogUpdates
from .chart_query import (
    COMPLETION_FIELDS,
    ChartSort,
    chart_filters,
    completion_matrix,
    filter_options,
    filtered,
    selection_summary,
    sort_charts,
)
from .clock import now
from .completion_exports import completion_export_router
from .exports import export_router
from .jackets import IMMUTABLE_CACHE, REVALIDATE_CACHE, JacketPreviews, etag_matches
from .score_sync import ScoreSync, ScoreSyncError
from .store import ROOT, Store
from .venue_locations import (
    PlaceSearchError,
    VenueLocationInput,
    confirmed_venue_charts,
    initialize_locations,
    save_location,
    search_places,
    venue_map,
)
from .venue_summary import summarize_venues

# Pydantic's default 422 echoes rejected input; never echo secrets or NaN values.
VALIDATION_MESSAGES = {
    "/api/auth/login": "Master key 格式无效。",
    "/api/settings/divingfish": "Token 格式无效，请检查输入。",
    "/api/settings/preferences": "默认定数须为 0–20 的有限数值，精确到 0.1，且下限不能高于上限。",
    "/api/venues/location": "地点信息无效，请检查名称、地址及经纬度。经纬度须同时填写或留空。",
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
    initialize_locations(store)
    score_sync = ScoreSync(store)
    bilibili = BilibiliService(store)
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
            score_sync.close()
            bilibili.close()
            store.close()

    app = FastAPI(title="Ashitemaru · maimai", version="0.1.0", lifespan=lifespan)
    auth = AuthStore(auth_config, store.runtime)
    public_frontend = public_paths(frontend)
    app.state.auth = auth
    app.include_router(auth_router(auth))
    app.state.store = store
    app.state.catalog_updates = updater
    app.state.jacket_previews = previews
    app.state.score_sync = score_sync
    app.state.bilibili = bilibili
    app.include_router(bilibili_router(bilibili))
    app.include_router(completion_export_router(store.runtime / "completion-exports"))
    app.include_router(export_router(store, assets))
    allowed_hosts = ["127.0.0.1", "localhost", "::1", "testserver"]
    allowed_hosts += [
        h.strip() for h in os.environ.get("MAIMAI_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(LoginBodyLimitMiddleware)

    @app.exception_handler(RequestValidationError)
    async def safe_validation(request: Request, exc):
        path = request.url.path
        message = (
            "投稿参数无效，请检查后重试。"
            if path.startswith("/api/bilibili/")
            else VALIDATION_MESSAGES.get(path)
        )
        if message:
            return JSONResponse({"detail": message}, status_code=422)
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

    @app.get("/api/catalog/records/review")
    def catalog_record_review():
        return updater.record_review()

    @app.post("/api/catalog/records/decisions/batch", status_code=202)
    def catalog_records_decide(value: CatalogRecordBatchInput):
        return begin_catalog_operation(lambda: updater.decide_records(value.model_dump()))

    @app.get("/api/catalog/records/decisions")
    def catalog_record_decisions():
        return updater.manual_records()

    @app.post("/api/catalog/records/decisions/revoke", status_code=202)
    def catalog_record_revoke(value: CatalogRecordRevokeInput):
        return begin_catalog_operation(lambda: updater.revoke_record(value.model_dump()))

    @app.post("/api/catalog/decisions/reapply", status_code=202)
    def reapply_catalog_decisions():
        return begin_catalog_operation(updater.reapply_decisions)

    def charts_matching(filters):
        return filtered(store.all_charts(), store.waterfish_map, **filters)

    @app.get("/api/version")
    def version():
        return {"token": store.version_token()}

    @app.get("/api/summary")
    def summary(filters: dict = Depends(chart_filters)):
        return selection_summary(charts_matching(filters), store.meta("last_sync"))

    @app.get("/api/chart-filters")
    def chart_filter_options(group: Literal["all", "low", "high", "catalog"] = "all"):
        return filter_options(store.all_charts(), store.waterfish_map, group)

    @app.get("/api/charts")
    def charts(
        filters: dict = Depends(chart_filters),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=6000),
        sort: ChartSort = "constant_desc",
    ):
        items = sort_charts(charts_matching(filters), sort)
        return {
            "items": public_many(items[offset : offset + limit]),
            "total": len(items),
            "offset": offset,
            "limit": limit,
        }

    @app.get("/api/completion-table")
    def completion_table(filters: dict = Depends(chart_filters), sort: ChartSort = "constant_desc"):
        items = sort_charts(charts_matching(filters), sort)
        return {
            "items": public_many(items, COMPLETION_FIELDS),
            "total": len(items),
            "summary": selection_summary(items, store.meta("last_sync")),
            "generated_at": now(),
        }

    @app.get("/api/completion-matrix")
    def completion_matrix_data(
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
        return completion_matrix(
            store.all_charts(),
            store.waterfish_map,
            difficulty=difficulty,
            constant_min=constant_min,
            constant_max=constant_max,
            cn_range=(cn_version_min, cn_version_max),
        )

    @app.get("/api/charts/{chart_key}")
    def chart(chart_key: str):
        item = store.chart(chart_key)
        if not item:
            raise HTTPException(404, "谱面不存在")
        return public(item)

    @app.get("/api/ap-records")
    def ap_records():
        records = store.ap_records()
        return {**records, "items": public_many(records["items"])}

    def record_conflict(chart_key):
        chart = store.chart(chart_key)
        return HTTPException(
            409,
            {
                "code": "record_conflict",
                "message": "记录已在其他页面更新，请重新载入后再保存。",
                "chart": public(chart) if chart else None,
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

    @app.get("/api/venues/map")
    def venue_map_data():
        result = venue_map(store)
        for item in result["items"]:
            item["jackets"] = public_many(item["jackets"])
        return result

    @app.get("/api/venues/charts")
    def venue_charts(
        name: str = Query(min_length=1, max_length=200),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=60, ge=1, le=100),
    ):
        items = confirmed_venue_charts(store.all_charts(), name)
        return {
            "items": public_many(items[offset : offset + limit]),
            "total": len(items),
            "offset": offset,
            "limit": limit,
        }

    @app.put("/api/venues/location")
    def update_venue_location(value: VenueLocationInput):
        return save_location(store, value)

    @app.get("/api/venues/search")
    def venue_search(
        q: str = Query(min_length=1, max_length=300),
        city: str = Query(default="", max_length=100),
    ):
        try:
            return search_places(store, q, city)
        except PlaceSearchError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from None

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

    @app.get("/api/sync/status")
    def score_sync_status():
        return score_sync.status()

    @app.post("/api/sync/divingfish", status_code=202)
    def start_divingfish_sync():
        try:
            return score_sync.start()
        except ScoreSyncError as exc:
            raise HTTPException(exc.status, exc.detail()) from None

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
