"""Bounded PNG-strip uploads and a single lossless downloadable PNG.

The browser renders fixed-size rows in small canvases. Only one strip is decoded
at a time here; the complete image is never allocated as an RGBA bitmap.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import tempfile
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from pathlib import Path

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

EXPORT_WIDTH = 3200
MAX_HEIGHT = 200_000
MAX_PARTS = 200
MAX_PART_HEIGHT = 8192
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_JOBS = 8
MAX_COMPLETED_JOBS = 8
MAX_STORAGE_BYTES = 8 * 1024 * 1024 * 1024
TTL_SECONDS = 24 * 60 * 60
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JOB_ID = re.compile(r"[0-9a-f]{32}\Z")


class ExportInput(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=180)
    width: int = Field(ge=EXPORT_WIDTH, le=EXPORT_WIDTH, strict=True)
    height: int = Field(ge=1, le=MAX_HEIGHT, strict=True)
    parts: int = Field(ge=1, le=MAX_PARTS, strict=True)

    @field_validator("name")
    @classmethod
    def filename_valid(cls, value):
        if (
            value != value.strip()
            or value in (".", "..")
            or any(ord(c) < 32 or ord(c) == 127 or c in '/\\<>:"|?*' for c in value)
            or not value.lower().endswith(".png")
        ):
            raise ValueError("导出文件名无效，必须是 PNG 文件名")
        return value


def _png_chunk(output, kind, data):
    output.write(struct.pack(">I", len(data)))
    output.write(kind)
    output.write(data)
    output.write(struct.pack(">I", zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF))


@contextmanager
def _rgba_strip(path, width, temporary_directory):
    """Keep canvas PNG filters, normalizing other PNG formats one strip at a time."""
    with path.open("rb") as encoded:
        header = encoded.read(29)
    direct = (
        header[:8] == PNG_SIGNATURE
        and header[12:16] == b"IHDR"
        and header[24:29] == b"\x08\x06\x00\x00\x00"
    )
    normalized = None
    try:
        with Image.open(path) as source:
            if source.width != width or not 1 <= source.height <= MAX_PART_HEIGHT:
                raise ValueError("PNG 分片尺寸不一致")
            source.load()
            rgba = source if source.mode == "RGBA" else source.convert("RGBA")
            try:
                strip_height = rgba.height
                with rgba.crop((0, 0, width, 1)) as row:
                    first_row = b"\0" + row.tobytes()
                if not direct:
                    normalized = tempfile.TemporaryFile(dir=temporary_directory)
                    rgba.save(normalized, format="PNG")
                    normalized.seek(0)
            finally:
                if rgba is not source:
                    rgba.close()
        if normalized is not None:
            yield normalized, first_row, strip_height
        else:
            with path.open("rb") as encoded:
                yield encoded, first_row, strip_height
    finally:
        if normalized is not None:
            normalized.close()


def _idat_blocks(source):
    """Read PNG chunks without allocating an entire IDAT payload."""
    if source.read(8) != PNG_SIGNATURE:
        raise ValueError("PNG 文件签名无效")
    while True:
        header = source.read(8)
        if len(header) != 8:
            raise ValueError("PNG 分片被截断")
        remaining, kind = struct.unpack(">I4s", header)
        crc = zlib.crc32(kind)
        while remaining:
            payload = source.read(min(remaining, 65536))
            if not payload:
                raise ValueError("PNG 分片被截断")
            crc = zlib.crc32(payload, crc)
            remaining -= len(payload)
            if kind == b"IDAT":
                yield payload
        checksum = source.read(4)
        if len(checksum) != 4 or struct.unpack(">I", checksum)[0] != crc & 0xFFFFFFFF:
            raise ValueError("PNG 分片校验失败")
        if kind == b"IEND":
            return


def _filtered_rows(source, width, height):
    """Inflate at most 64 KiB per step, preserving each scanline's PNG filter."""
    inflater = zlib.decompressobj()
    pending = bytearray()
    row_bytes = width * 4 + 1
    count = 0
    try:
        for compressed in _idat_blocks(source):
            while True:
                decoded = inflater.decompress(compressed, 65536)
                if inflater.unused_data:
                    raise ValueError("PNG 分片包含多余压缩数据")
                pending.extend(decoded)
                while len(pending) >= row_bytes:
                    count += 1
                    if count > height or pending[0] > 4:
                        raise ValueError("PNG 分片扫描行无效")
                    yield bytes(pending[:row_bytes])
                    del pending[:row_bytes]
                compressed = inflater.unconsumed_tail
                if not compressed and len(decoded) < 65536:
                    break
    except zlib.error:
        raise ValueError("PNG 分片压缩数据无效") from None
    if not inflater.eof or pending or count != height:
        raise ValueError("PNG 分片扫描行数不一致")


def assemble_png(paths, destination, width, height):
    """Join lossless filtered scanlines into one bounded, continuous zlib stream."""
    compressor = zlib.compressobj(level=6)
    pending = bytearray()
    rows_written = 0
    with destination.open("wb") as output:
        output.write(PNG_SIGNATURE)
        _png_chunk(output, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))

        def emit(data):
            pending.extend(data)
            while len(pending) >= 65536:
                _png_chunk(output, b"IDAT", pending[:65536])
                del pending[:65536]

        for path in paths:
            with _rgba_strip(path, width, destination.parent) as (source, first_row, strip_height):
                for y, row in enumerate(_filtered_rows(source, width, strip_height)):
                    # A strip's first filter used an all-zero previous row. Reset
                    # that row so joining it after another strip keeps every pixel.
                    emit(compressor.compress(first_row if y == 0 else row))
                rows_written += strip_height
        if rows_written != height:
            raise ValueError("PNG 分片总高度不一致")
        emit(compressor.flush())
        if pending:
            _png_chunk(output, b"IDAT", pending)
        _png_chunk(output, b"IEND", b"")


class CompletionExports:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        # Busy jobs cannot expire or be modified during upload/finalization.
        self.reserved = {}

    def _directory(self, job_id):
        if not JOB_ID.fullmatch(job_id):
            raise HTTPException(404, "导出任务不存在")
        return self.root / job_id

    def _load(self, job_id):
        directory = self._directory(job_id)
        try:
            manifest = json.loads((directory / "manifest.json").read_text())
        except (OSError, ValueError):
            raise HTTPException(404, "导出任务不存在或已过期") from None
        if time.time() - manifest["touched"] > TTL_SECONDS and job_id not in self.reserved:
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(404, "导出任务已过期，请重新导出")
        return directory, manifest

    def _save(self, directory, manifest):
        manifest["touched"] = time.time()
        temporary = directory / "manifest.tmp"
        temporary.write_text(json.dumps(manifest, ensure_ascii=False))
        temporary.replace(directory / "manifest.json")

    def _cleanup(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        completed = []
        for directory in self.root.iterdir():
            if (
                not directory.is_dir()
                or not JOB_ID.fullmatch(directory.name)
                or directory.name in self.reserved
            ):
                continue
            try:
                _, manifest = self._load(directory.name)
                if manifest["complete"]:
                    completed.append((manifest["touched"], directory))
            except HTTPException:
                shutil.rmtree(directory, ignore_errors=True)
        for _, directory in sorted(completed)[:-MAX_COMPLETED_JOBS]:
            shutil.rmtree(directory, ignore_errors=True)

    def _check_storage(self, additional):
        used = sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        if used + sum(self.reserved.values()) + additional > MAX_STORAGE_BYTES:
            raise HTTPException(507, "导出临时空间已满，请稍后重试")

    def create(self, value):
        if not value.parts <= value.height <= value.parts * MAX_PART_HEIGHT:
            raise HTTPException(422, "PNG 分片数与总高度不匹配")
        with self.lock:
            self._cleanup()
            active = sum(
                not self._load(p.name)[1]["complete"]
                for p in self.root.iterdir()
                if p.is_dir() and JOB_ID.fullmatch(p.name)
            )
            if active >= MAX_JOBS:
                raise HTTPException(429, "导出任务过多，请稍后重试")
            self._check_storage(0)
            job_id = uuid.uuid4().hex
            directory = self._directory(job_id)
            directory.mkdir(mode=0o700)
            self._save(directory, {**value.model_dump(), "segments": {}, "complete": False})
            return {"id": job_id}

    def begin_upload(self, job_id, index):
        with self.lock:
            directory, manifest = self._load(job_id)
            if not 0 <= index < manifest["parts"]:
                raise HTTPException(404, "PNG 分片序号无效")
            if job_id in self.reserved or manifest["complete"]:
                raise HTTPException(409, "导出任务正在处理或已完成")
            self._check_storage(MAX_PART_BYTES)
            self.reserved[job_id] = MAX_PART_BYTES
            return directory / f"{index}.upload"

    def accept_upload(self, job_id, index, temporary):
        try:
            with Image.open(temporary) as source:
                if source.format != "PNG" or getattr(source, "n_frames", 1) != 1:
                    raise ValueError("必须上传静态 PNG 分片")
                if source.width != EXPORT_WIDTH or not 1 <= source.height <= MAX_PART_HEIGHT:
                    raise ValueError("PNG 分片必须宽 3200 像素，高度不超过 8192 像素")
                height = source.height
                # Verify CRCs without retaining a decoded strip in memory.
                source.verify()
            # CRC validation alone does not validate the compressed pixel stream.
            with Image.open(temporary) as source:
                source.load()
        except (
            OSError,
            ValueError,
            SyntaxError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ) as exc:
            raise HTTPException(422, f"PNG 分片无效：{exc}") from None
        with self.lock:
            directory, manifest = self._load(job_id)
            segments = {**manifest["segments"], str(index): height}
            total = sum(segments.values())
            remaining = manifest["parts"] - len(segments)
            if not total + remaining <= manifest["height"] <= total + remaining * MAX_PART_HEIGHT:
                raise HTTPException(422, "PNG 分片总高度与导出尺寸不匹配")
            temporary.replace(directory / f"{index}.png")
            manifest["segments"] = segments
            self._save(directory, manifest)

    def end_upload(self, job_id, temporary):
        with self.lock:
            temporary.unlink(missing_ok=True)
            self.reserved.pop(job_id, None)

    def finish(self, job_id):
        with self.lock:
            directory, manifest = self._load(job_id)
            result = {"url": f"/api/completion-exports/{job_id}/image", "name": manifest["name"]}
            if manifest["complete"]:
                return result
            if job_id in self.reserved:
                raise HTTPException(409, "导出任务正在处理，请稍后重试")
            if (
                len(manifest["segments"]) != manifest["parts"]
                or sum(manifest["segments"].values()) != manifest["height"]
            ):
                raise HTTPException(409, "PNG 分片尚未上传完整")
            # Reserve a conservative upper bound for an incompressible RGBA PNG.
            reserve = (
                manifest["width"] * manifest["height"] * 4
                + manifest["height"]
                + EXPORT_WIDTH * MAX_PART_HEIGHT * 4
                + 8 * 1024 * 1024
            )
            self._check_storage(reserve)
            self.reserved[job_id] = reserve
        temporary = directory / "image.tmp"
        try:
            paths = [directory / f"{index}.png" for index in range(manifest["parts"])]
            assemble_png(paths, temporary, manifest["width"], manifest["height"])
            with self.lock:
                temporary.replace(directory / "image.png")
                manifest["complete"] = True
                self._save(directory, manifest)
                for path in paths:
                    path.unlink(missing_ok=True)
            return result
        except (OSError, ValueError):
            raise HTTPException(500, "PNG 合成失败，已保留分片，可重试导出") from None
        finally:
            with self.lock:
                temporary.unlink(missing_ok=True)
                self.reserved.pop(job_id, None)

    def image(self, job_id):
        with self.lock:
            directory, manifest = self._load(job_id)
            if not manifest["complete"] or not (directory / "image.png").is_file():
                raise HTTPException(409, "PNG 尚未合成完成")
            self._save(directory, manifest)
            return FileResponse(
                directory / "image.png", media_type="image/png", filename=manifest["name"]
            )


def completion_export_router(root):
    exports = CompletionExports(root)
    router = APIRouter(prefix="/api/completion-exports")

    @router.post("")
    def create_export(value: ExportInput):
        return exports.create(value)

    @router.put("/{job_id}/parts/{index}")
    async def upload_part(job_id: str, index: int, request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "image/png":
            raise HTTPException(415, "请上传 image/png 分片")
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                raise HTTPException(400, "Content-Length 无效") from None
            if length < 0:
                raise HTTPException(400, "Content-Length 无效")
            if length > MAX_PART_BYTES:
                raise HTTPException(413, "PNG 分片超过 64 MiB")
        temporary = await run_in_threadpool(exports.begin_upload, job_id, index)
        try:
            received = 0
            async with await anyio.open_file(temporary, "wb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > MAX_PART_BYTES:
                        raise HTTPException(413, "PNG 分片超过 64 MiB")
                    await output.write(chunk)
            await run_in_threadpool(exports.accept_upload, job_id, index, temporary)
            return {"ok": True}
        finally:
            await run_in_threadpool(exports.end_upload, job_id, temporary)

    @router.post("/{job_id}/finish")
    def finish_export(job_id: str):
        # FastAPI executes this synchronous endpoint in its worker thread pool.
        return exports.finish(job_id)

    @router.get("/{job_id}/image")
    def download_export(job_id: str):
        return exports.image(job_id)

    return router
