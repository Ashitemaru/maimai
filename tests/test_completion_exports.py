import io
import random
import struct
import tempfile
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

    def create(self):
        response = self.client.post("/api/completion-exports")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def png(self, color=(20, 40, 60, 255), height=3, width=3200, mode="RGBA"):
        buffer = io.BytesIO()
        with Image.new(mode, (width, height), color) as image:
            image.save(buffer, format="PNG")
        return buffer.getvalue()

    def upload(self, job_id, content, index=0):
        return self.client.put(f"/api/completion-exports/{job_id}/parts/{index}", content=content)

    def finish(self, job_id, parts=1):
        return self.client.post(f"/api/completion-exports/{job_id}/finish", json={"parts": parts})

    def export(self, *strips):
        job_id = self.create()
        for index, content in enumerate(strips):
            self.assertEqual(self.upload(job_id, content, index).status_code, 200)
        response = self.finish(job_id, len(strips))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertFalse((self.root / job_id).exists(), "parts are removed after stitching")
        return response.content

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

    def test_strip_joins_preserve_pixels_and_png_is_valid(self):
        colors = [(255, 0, 0, 255), (0, 255, 0, 128), (0, 0, 255, 255)]
        content = self.export(*(self.png(colors[i], height=h) for i, h in enumerate((2, 3, 1))))
        with Image.open(io.BytesIO(content)) as image:
            self.assertEqual(image.size, (3200, 6))
            for y, color in enumerate([colors[0]] * 2 + [colors[1]] * 3 + [colors[2]]):
                self.assertEqual(image.getpixel((0, y)), color)
                self.assertEqual(image.getpixel((3199, y)), color)
        offset = 8
        compressed = bytearray()
        while offset < len(content):
            length, kind = struct.unpack(">I4s", content[offset : offset + 8])
            payload = content[offset + 8 : offset + 8 + length]
            (crc,) = struct.unpack(">I", content[offset + 8 + length : offset + 12 + length])
            self.assertEqual(crc, zlib.crc32(payload, zlib.crc32(kind)))
            if kind == b"IDAT":
                compressed.extend(payload)
            offset += 12 + length
        self.assertEqual(len(zlib.decompress(compressed)), 6 * (3200 * 4 + 1))

    def test_nonzero_first_row_filters_and_alpha_preserve_all_pixels_across_joins(self):
        expected = bytearray()
        strips = []
        for index, first_filter in enumerate((1, 2, 3, 4)):
            rows = [
                bytes((x * 7 + y * 31 + index * 79) % 256 for x in range(3200 * 4))
                for y in range(4)
            ]
            strips.append(self.filtered_png(rows, [first_filter, 2, 3, 4]))
            expected.extend(b"".join(rows))
        with Image.open(io.BytesIO(self.export(*strips))) as image:
            self.assertEqual(image.size, (3200, 16))
            self.assertEqual(image.tobytes(), bytes(expected))

    def test_retaining_png_filters_keeps_textured_strips_small(self):
        rng = random.Random(109)
        base = bytes(rng.randrange(256) for _ in range(3200 * 4))
        rows = [bytes((value + y) % 256 for value in base) for y in range(100)]
        content = self.filtered_png(rows, [0] + [2] * 99)
        result = self.export(content, content)
        unfiltered_size = len(zlib.compress(b"".join(b"\0" + row for row in rows) * 2))
        self.assertLess(len(result), unfiltered_size // 10)
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.tobytes(), b"".join(rows) * 2)

    def test_rgb_and_palette_strips_are_normalized_losslessly(self):
        palette = io.BytesIO()
        with Image.new("P", (3200, 2)) as image:
            image.putpalette([255, 30, 20, 10, 240, 50] + [0] * (256 * 3 - 6))
            image.frombytes(bytes([0, 1]) * 3200)
            image.save(palette, format="PNG", transparency=0)
        content = self.export(self.png((17, 45, 201), mode="RGB"), palette.getvalue())
        with Image.open(io.BytesIO(content)) as image:
            self.assertEqual(image.getpixel((1234, 2)), (17, 45, 201, 255))
            self.assertEqual(image.getpixel((0, 3)), (255, 30, 20, 0))
            self.assertEqual(image.getpixel((1, 3)), (10, 240, 50, 255))

    def test_invalid_parts_are_rejected_and_cleaned(self):
        self.assertEqual(self.finish("invalid").status_code, 404)
        self.assertEqual(self.finish("a" * 32).status_code, 404)
        self.assertEqual(self.upload("a" * 32, self.png()).status_code, 404)
        for content in (b"not a png", self.png(width=3199), self.png(height=4097)):
            with self.subTest(size=len(content)):
                job_id = self.create()
                self.assertEqual(self.upload(job_id, content).status_code, 200)
                self.assertEqual(self.finish(job_id).status_code, 422)
                self.assertFalse((self.root / job_id).exists())
        job_id = self.create()
        self.assertEqual(self.upload(job_id, self.png(), index=module.MAX_PARTS).status_code, 404)
        self.upload(job_id, self.png())
        self.assertEqual(self.finish(job_id, parts=2).status_code, 422, "missing part")
        for parts in (0, module.MAX_PARTS + 1):
            self.assertEqual(self.finish(self.create(), parts=parts).status_code, 422)

    def test_total_height_is_capped(self):
        job_id = self.create()
        self.upload(job_id, self.png(height=3))
        with patch.object(module, "MAX_HEIGHT", 2):
            self.assertEqual(self.finish(job_id).status_code, 422)

    def test_part_size_limit_removes_partial_upload(self):
        job_id = self.create()
        with patch.object(module, "MAX_PART_BYTES", 32):
            response = self.client.put(
                f"/api/completion-exports/{job_id}/parts/0",
                content=iter([b"a" * 16, b"b" * 17]),
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(list((self.root / job_id).iterdir()), [])

    def test_only_one_export_is_kept(self):
        stale = self.create()
        self.upload(stale, self.png())
        current = self.create()
        self.assertFalse((self.root / stale).exists(), "a new export discards the old one")
        self.assertEqual(self.upload(stale, self.png()).status_code, 404)
        self.assertEqual(self.upload(current, self.png()).status_code, 200)
        self.assertEqual(self.finish(current).status_code, 200)
        self.assertEqual(list(self.root.iterdir()), [])
        (self.root / current).mkdir()
        module.completion_export_router(self.root)
        self.assertFalse(self.root.exists(), "startup removes leftovers")


if __name__ == "__main__":
    unittest.main()
