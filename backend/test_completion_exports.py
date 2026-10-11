import io
import json
import random
import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend import completion_exports as module


class CompletionExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "exports"
        app = FastAPI()
        app.include_router(module.completion_export_router(self.root))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def create(self, height=3, parts=1, **changes):
        return self.client.post(
            "/api/completion-exports",
            json={
                "name": "完成表.png",
                "width": 3200,
                "height": height,
                "parts": parts,
                **changes,
            },
        )

    def png(self, color=(20, 40, 60, 255), height=3, width=3200, mode="RGBA"):
        buffer = io.BytesIO()
        with Image.new(mode, (width, height), color) as image:
            image.save(buffer, format="PNG")
        return buffer.getvalue()

    def upload(self, job_id, content, index=0, **headers):
        return self.client.put(
            f"/api/completion-exports/{job_id}/parts/{index}",
            content=content,
            headers={"Content-Type": "image/png", **headers},
        )

    def finish(self, job_id):
        return self.client.post(f"/api/completion-exports/{job_id}/finish")

    def filtered_png(self, rows, filters):
        """Make controlled PNG filters, including nonzero first-row predictors."""

        def paeth(left, above, upper_left):
            prediction = left + above - upper_left
            distances = (
                abs(prediction - left),
                abs(prediction - above),
                abs(prediction - upper_left),
            )
            return (left, above, upper_left)[distances.index(min(distances))]

        scanlines = bytearray()
        previous = bytes(3200 * 4)
        for row, kind in zip(rows, filters, strict=True):
            scanlines.append(kind)
            for x, value in enumerate(row):
                left = row[x - 4] if x >= 4 else 0
                above = previous[x]
                upper_left = previous[x - 4] if x >= 4 else 0
                predictor = (0, left, above, (left + above) // 2, paeth(left, above, upper_left))[
                    kind
                ]
                scanlines.append((value - predictor) % 256)
            previous = row
        output = io.BytesIO()
        output.write(module.PNG_SIGNATURE)
        module._png_chunk(output, b"IHDR", struct.pack(">IIBBBBB", 3200, len(rows), 8, 6, 0, 0, 0))
        compressed = zlib.compress(scanlines)
        for index in range(0, len(compressed), 113):
            module._png_chunk(output, b"IDAT", compressed[index : index + 113])
        module._png_chunk(output, b"IEND", b"")
        return output.getvalue()

    def test_roundtrip_preserves_pixels_at_strip_joins_and_downloads_one_png(self):
        job_id = self.create(height=6, parts=3).json()["id"]
        colors = [(255, 0, 0, 255), (0, 255, 0, 128), (0, 0, 255, 255)]
        for index, height in enumerate((2, 3, 1)):
            result = self.upload(job_id, self.png(colors[index], height=height), index)
            self.assertEqual(result.status_code, 200, result.text)
        result = self.finish(job_id)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.finish(job_id).json(), result.json())
        download = self.client.get(result.json()["url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers["content-type"], "image/png")
        self.assertIn("attachment;", download.headers["content-disposition"])
        self.assertIn("filename*=utf-8''", download.headers["content-disposition"])
        with Image.open(io.BytesIO(download.content)) as image:
            self.assertEqual(image.size, (3200, 6))
            for y, color in enumerate([colors[0]] * 2 + [colors[1]] * 3 + [colors[2]]):
                self.assertEqual(image.getpixel((0, y)), color)
                self.assertEqual(image.getpixel((3199, y)), color)
        offset = 8
        compressed = bytearray()
        while offset < len(download.content):
            length, kind = struct.unpack(">I4s", download.content[offset : offset + 8])
            payload = download.content[offset + 8 : offset + 8 + length]
            (crc,) = struct.unpack(
                ">I", download.content[offset + 8 + length : offset + 12 + length]
            )
            self.assertEqual(crc, zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF)
            if kind == b"IDAT":
                self.assertLessEqual(length, 65536)
                compressed.extend(payload)
            offset += 12 + length
        self.assertEqual(len(zlib.decompress(compressed)), 6 * (3200 * 4 + 1))
        self.assertEqual(list((self.root / job_id).glob("[0-9]*.png")), [])

    def test_rgb_strip_is_converted_without_changing_color(self):
        job_id = self.create().json()["id"]
        self.assertEqual(self.upload(job_id, self.png((17, 45, 201), mode="RGB")).status_code, 200)
        response = self.client.get(self.finish(job_id).json()["url"])
        with Image.open(io.BytesIO(response.content)) as image:
            self.assertEqual(image.getpixel((1234, 2)), (17, 45, 201, 255))

    def test_nonzero_first_row_filters_and_alpha_preserve_all_pixels_across_joins(self):
        job_id = self.create(height=16, parts=4).json()["id"]
        expected = bytearray()
        for index, first_filter in enumerate((1, 2, 3, 4)):
            rows = [
                bytes((x * 7 + y * 31 + index * 79) % 256 for x in range(3200 * 4))
                for y in range(4)
            ]
            content = self.filtered_png(rows, [first_filter, 2, 3, 4])
            self.assertEqual(self.upload(job_id, content, index).status_code, 200)
            expected.extend(b"".join(rows))
        result = self.finish(job_id)
        self.assertEqual(result.status_code, 200, result.text)
        download = self.client.get(result.json()["url"])
        with Image.open(io.BytesIO(download.content)) as image:
            self.assertEqual(image.size, (3200, 16))
            self.assertEqual(image.tobytes(), bytes(expected))

    def test_retaining_png_filters_keeps_textured_strips_small(self):
        rng = random.Random(109)
        base = bytes(rng.randrange(256) for _ in range(3200 * 4))
        rows = [bytes((value + y) % 256 for value in base) for y in range(100)]
        content = self.filtered_png(rows, [0] + [2] * 99)
        job_id = self.create(height=200, parts=2).json()["id"]
        for index in range(2):
            self.assertEqual(self.upload(job_id, content, index).status_code, 200)
        response = self.client.get(self.finish(job_id).json()["url"])
        unfiltered_size = len(zlib.compress(b"".join(b"\0" + row for row in rows) * 2))
        self.assertLess(len(response.content), unfiltered_size // 10)
        with Image.open(io.BytesIO(response.content)) as image:
            self.assertEqual(image.tobytes(), b"".join(rows) * 2)

    def test_palette_transparency_is_normalized_losslessly(self):
        output = io.BytesIO()
        with Image.new("P", (3200, 2)) as image:
            image.putpalette([255, 30, 20, 10, 240, 50] + [0] * (256 * 3 - 6))
            image.frombytes(bytes([0, 1]) * 3200)
            image.save(output, format="PNG", transparency=0)
        job_id = self.create(height=2).json()["id"]
        self.assertEqual(self.upload(job_id, output.getvalue()).status_code, 200)
        response = self.client.get(self.finish(job_id).json()["url"])
        with Image.open(io.BytesIO(response.content)) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 30, 20, 0))
            self.assertEqual(image.getpixel((1, 0)), (10, 240, 50, 255))

    def test_invalid_dimensions_filenames_and_identifiers(self):
        for values in (
            {"width": 1600},
            {"height": 200001},
            {"parts": 201},
            {"height": 8193},
            {"height": 1, "parts": 2},
            {"name": "../escape.png"},
            {"name": "a\r\n.png"},
            {"name": "a.jpg"},
        ):
            with self.subTest(values=values):
                self.assertEqual(self.create(**values).status_code, 422)
        self.assertEqual(self.finish("invalid").status_code, 404)
        self.assertEqual(self.finish("a" * 32).status_code, 404)

    def test_missing_wrong_height_and_invalid_segments_are_rejected(self):
        job_id = self.create(height=5, parts=2).json()["id"]
        self.assertEqual(self.finish(job_id).status_code, 409)
        self.assertEqual(self.upload(job_id, b"not a png").status_code, 422)
        self.assertEqual(self.upload(job_id, self.png(width=3199)).status_code, 422)
        self.assertEqual(self.upload(job_id, self.png(height=6)).status_code, 422)
        self.assertEqual(self.upload(job_id, self.png(), index=2).status_code, 404)
        self.assertEqual(
            self.upload(job_id, self.png(), **{"Content-Type": "image/jpeg"}).status_code, 415
        )
        self.assertEqual(self.upload(job_id, self.png(height=3)).status_code, 200)
        self.assertEqual(self.upload(job_id, self.png(height=1), index=1).status_code, 422)
        self.assertEqual(self.finish(job_id).status_code, 409)
        self.assertEqual(self.upload(job_id, self.png(height=2), index=1).status_code, 200)
        self.assertEqual(self.finish(job_id).status_code, 200)
        self.assertEqual(self.upload(job_id, self.png()).status_code, 409)

    def test_body_limit_catches_declared_and_streamed_size_and_cleans_up(self):
        job_id = self.create().json()["id"]
        with patch.object(module, "MAX_PART_BYTES", 32):
            self.assertEqual(self.upload(job_id, b"a" * 33).status_code, 413)
            response = self.client.put(
                f"/api/completion-exports/{job_id}/parts/0",
                content=iter([b"a" * 16, b"b" * 17]),
                headers={"Content-Type": "image/png"},
            )
            self.assertEqual(response.status_code, 413)
        self.assertFalse((self.root / job_id / "0.upload").exists())
        self.assertEqual(self.upload(job_id, self.png()).status_code, 200)

    def test_finalize_failure_retains_parts_and_can_be_retried(self):
        job_id = self.create().json()["id"]
        self.upload(job_id, self.png())
        with patch.object(module, "assemble_png", side_effect=OSError("temporary disk failure")):
            self.assertEqual(self.finish(job_id).status_code, 500)
        self.assertTrue((self.root / job_id / "0.png").is_file())
        self.assertFalse((self.root / job_id / "image.png").exists())
        self.assertEqual(self.finish(job_id).status_code, 200)

    def test_expired_and_excess_jobs_and_storage_are_bounded(self):
        job_id = self.create().json()["id"]
        with patch.object(module, "MAX_JOBS", 1):
            self.assertEqual(self.create().status_code, 429)
        with patch.object(module, "MAX_STORAGE_BYTES", 1):
            self.assertEqual(self.upload(job_id, self.png()).status_code, 507)
        manifest_path = self.root / job_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["touched"] = time.time() - module.TTL_SECONDS - 1
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(self.finish(job_id).status_code, 404)
        self.assertFalse(manifest_path.parent.exists())
        self.assertEqual(self.create().status_code, 200)

    def test_completed_jobs_do_not_exhaust_active_job_limit(self):
        with patch.object(module, "MAX_JOBS", 1), patch.object(module, "MAX_COMPLETED_JOBS", 1):
            completed = []
            for _ in range(3):
                response = self.create()
                self.assertEqual(response.status_code, 200, response.text)
                job_id = response.json()["id"]
                self.upload(job_id, self.png())
                self.assertEqual(self.finish(job_id).status_code, 200)
                completed.append(job_id)
            self.assertFalse((self.root / completed[0]).exists())


if __name__ == "__main__":
    unittest.main()
