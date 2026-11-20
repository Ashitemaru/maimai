"""Stitch browser-rendered PNG strips into one tall PNG.

iPhone Safari cannot allocate one canvas for the whole completion table, so the
browser uploads fixed-width strips and the server joins them. Only one export
exists at a time; starting a new one discards any abandoned parts.
"""

from __future__ import annotations

import io
import re
import shutil
import struct
import tempfile
import uuid
import zlib
from pathlib import Path

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from PIL import Image
from pydantic import BaseModel, Field

EXPORT_WIDTH = 3200
MAX_HEIGHT = 200_000
MAX_PART_HEIGHT = 4096
MAX_PARTS = -(-MAX_HEIGHT // MAX_PART_HEIGHT)
MAX_PART_BYTES = 64 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RGBA8 = b"\x08\x06\x00\x00\x00"  # 8-bit RGBA, deflate, adaptive filters, not interlaced
ROW_BYTES = EXPORT_WIDTH * 4 + 1
JOB_ID = re.compile(r"[0-9a-f]{32}\Z")


class FinishInput(BaseModel):
    parts: int = Field(ge=1, le=MAX_PARTS, strict=True)


def _png_chunk(output, kind, data):
    output.write(struct.pack(">I", len(data)) + kind + data)
    output.write(struct.pack(">I", zlib.crc32(data, zlib.crc32(kind))))


def _read_strip(path):
    """Return (height, unfiltered first row, filtered scanlines) of one strip."""
    data = path.read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG" or image.width != EXPORT_WIDTH:
            raise ValueError("PNG 分片必须宽 3200 像素")
        if not 1 <= image.height <= MAX_PART_HEIGHT:
            raise ValueError("PNG 分片高度无效")
        image.load()
        rgba = image if image.mode == "RGBA" else image.convert("RGBA")
        height = rgba.height
        first_row = b"\0" + rgba.crop((0, 0, EXPORT_WIDTH, 1)).tobytes()
        if data[12:16] != b"IHDR" or data[24:29] != RGBA8:
            # Canvas output is already RGBA8; anything else is re-encoded once.
            buffer = io.BytesIO()
            rgba.save(buffer, format="PNG")
            data = buffer.getvalue()
    idat = []
    position = len(PNG_SIGNATURE)
    while position < len(data):
        length, kind = struct.unpack_from(">I4s", data, position)
        if kind == b"IDAT":
            idat.append(data[position + 8 : position + 8 + length])
        position += length + 12
    rows = zlib.decompress(b"".join(idat))
    if len(rows) != height * ROW_BYTES:
        raise ValueError("PNG 分片扫描行数不一致")
    return height, first_row, rows


def assemble_png(paths, output):
    """Join strips losslessly, keeping the browser's per-scanline PNG filters."""
    output.write(PNG_SIGNATURE)
    _png_chunk(output, b"IHDR", bytes(13))  # Rewritten once the total height is known.
    compressor = zlib.compressobj(level=6)
    height = 0

    def emit(data):
        if data:
            _png_chunk(output, b"IDAT", data)

    for path in paths:
        strip_height, first_row, rows = _read_strip(path)
        height += strip_height
        if height > MAX_HEIGHT:
            raise ValueError("导出图片超过 200000 像素")
        # The first row was filtered against an all-zero previous row; store it
        # unfiltered so it stays exact below the previous strip's last row.
        emit(compressor.compress(first_row))
        emit(compressor.compress(memoryview(rows)[ROW_BYTES:]))
    emit(compressor.flush())
    _png_chunk(output, b"IEND", b"")
    output.seek(len(PNG_SIGNATURE))
    _png_chunk(output, b"IHDR", struct.pack(">II", EXPORT_WIDTH, height) + RGBA8)


def completion_export_router(root):
    root = Path(root)
    shutil.rmtree(root, ignore_errors=True)
    router = APIRouter(prefix="/api/completion-exports")

    def job_directory(job_id):
        directory = root / job_id
        if not JOB_ID.fullmatch(job_id) or not directory.is_dir():
            raise HTTPException(404, "导出任务不存在，请重新导出")
        return directory

    @router.post("")
    def create_export():
        shutil.rmtree(root, ignore_errors=True)
        job_id = uuid.uuid4().hex
        (root / job_id).mkdir(parents=True, mode=0o700)
        return {"id": job_id}

    @router.put("/{job_id}/parts/{index}")
    async def upload_part(job_id: str, index: int, request: Request):
        if not 0 <= index < MAX_PARTS:
            raise HTTPException(404, "PNG 分片序号无效")
        path = job_directory(job_id) / f"{index}.png"
        received = 0
        try:
            async with await anyio.open_file(path, "wb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > MAX_PART_BYTES:
                        raise HTTPException(413, "PNG 分片超过 64 MiB")
                    await output.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"ok": True}

    @router.post("/{job_id}/finish")
    def finish_export(job_id: str, value: FinishInput):
        directory = job_directory(job_id)
        output = tempfile.TemporaryFile(dir=root)
        try:
            assemble_png([directory / f"{index}.png" for index in range(value.parts)], output)
        except (OSError, SyntaxError, ValueError, zlib.error):
            output.close()
            raise HTTPException(422, "PNG 分片缺失或无效，请重新导出") from None
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        size = output.seek(0, 2)
        output.seek(0)

        def stream():
            with output:
                while chunk := output.read(1024 * 1024):
                    yield chunk

        return StreamingResponse(
            stream(), media_type="image/png", headers={"Content-Length": str(size)}
        )

    return router
