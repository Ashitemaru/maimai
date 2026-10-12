"""Small, versioned jacket previews; original artwork remains untouched."""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

from PIL import Image, UnidentifiedImageError

PREVIEW_SIZE = 160
ENCODER_VERSION = "160-webp-q82-v1"
IMMUTABLE_CACHE = "private, max-age=31536000, immutable"
REVALIDATE_CACHE = "private, max-age=0, must-revalidate"
_FILENAME = re.compile(r"[0-9]{1,20}\.png")
_CACHED_FILE = re.compile(r"[0-9]{1,20}-[a-f0-9]{24}\.webp")


def etag_matches(header: str | None, etag: str) -> bool:
    return any(
        value.strip().removeprefix("W/") in (etag, "*") for value in (header or "").split(",")
    )


class JacketPreviews:
    """Persist derived images, cap disk use, and coalesce concurrent cache misses."""

    def __init__(
        self, directory, source_directories, *, max_bytes=64 * 1024 * 1024, max_files=4096
    ):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.source_directories = source_directories
        self.max_bytes = max_bytes
        self.max_files = max_files
        self._lock = threading.Lock()
        self._render_slots = threading.BoundedSemaphore(2)
        self._key_locks = [threading.Lock() for _ in range(64)]
        self._files = OrderedDict()
        self._bytes = 0
        existing = []
        for path in self.directory.iterdir():
            if _CACHED_FILE.fullmatch(path.name) and not path.is_symlink() and path.is_file():
                stat = path.stat()
                existing.append((stat.st_mtime_ns, path.name, stat.st_size))
        for _, name, size in sorted(existing):
            self._files[name] = size
            self._bytes += size
        with self._lock:
            self._prune()

    def source(self, filename):
        if not _FILENAME.fullmatch(filename):
            raise FileNotFoundError(filename)
        for directory in self.source_directories():
            path = directory / "jackets" / filename
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if not path.is_file():
                continue
            # ctime also invalidates replacements that deliberately preserve mtime.
            identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            revision = hashlib.sha256(repr((ENCODER_VERSION, identity)).encode()).hexdigest()[:24]
            return path, revision
        raise FileNotFoundError(filename)

    def url(self, original_url):
        match = re.fullmatch(r"/jackets/([0-9]{1,20})\.png", original_url or "")
        if not match:
            return None
        try:
            _, revision = self.source(match[1] + ".png")
        except FileNotFoundError:
            return None
        return f"/jackets/thumbnails/{match[1]}/{revision}.webp"

    def _prune(self):
        while self._files and (len(self._files) > self.max_files or self._bytes > self.max_bytes):
            name, size = self._files.popitem(last=False)
            self._bytes -= size
            (self.directory / name).unlink(missing_ok=True)

    def _read(self, name):
        with self._lock:
            if name not in self._files:
                return None
            try:
                content = (self.directory / name).read_bytes()
            except FileNotFoundError:
                self._bytes -= self._files.pop(name)
                return None
            self._files.move_to_end(name)
            return content

    @staticmethod
    def _render(path):
        try:
            with Image.open(path) as source:
                if source.width * source.height > 16_000_000:
                    raise ValueError("Jacket dimensions exceed the preview limit")
                image = source.convert("RGBA" if source.has_transparency_data else "RGB")
                image.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, format="WEBP", quality=82, method=4)
                return output.getvalue()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise FileNotFoundError("Jacket cannot be decoded") from exc

    def image(self, song_id, revision):
        if not re.fullmatch(r"[a-f0-9]{24}", revision):
            raise FileNotFoundError(revision)
        source, current = self.source(song_id + ".png")
        if current != revision:
            raise FileNotFoundError("Jacket revision changed")
        name = f"{song_id}-{revision}.webp"
        content = self._read(name)
        if content is not None:
            return content
        with self._key_locks[int(revision[:4], 16) % len(self._key_locks)]:
            content = self._read(name)
            if content is not None:
                return content
            with self._render_slots:
                content = self._render(source)
            if self.source(song_id + ".png")[1] != revision:
                raise FileNotFoundError("Jacket changed during rendering")
            if len(content) > self.max_bytes or self.max_files < 1:
                return content
            descriptor, temporary = tempfile.mkstemp(
                prefix=".thumb-", suffix=".tmp", dir=self.directory
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                with self._lock:
                    os.replace(temporary, self.directory / name)
                    self._bytes -= self._files.pop(name, 0)
                    self._files[name] = len(content)
                    self._bytes += len(content)
                    self._prune()
            finally:
                Path(temporary).unlink(missing_ok=True)
            return content
