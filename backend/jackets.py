"""Small, versioned jacket previews; original artwork remains untouched."""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import threading
from pathlib import Path

from PIL import Image, UnidentifiedImageError

PREVIEW_SIZE = 160
ENCODER_VERSION = "160-webp-q82-v1"
IMMUTABLE_CACHE = "private, max-age=31536000, immutable"
REVALIDATE_CACHE = "private, max-age=0, must-revalidate"
_FILENAME = re.compile(r"[0-9]{1,20}\.png")


def etag_matches(header: str | None, etag: str) -> bool:
    return any(
        value.strip().removeprefix("W/") in (etag, "*") for value in (header or "").split(",")
    )


class JacketPreviews:
    """160px WebP previews cached on disk, one file per jacket and source revision."""

    def __init__(self, directory, source_directories):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.source_directories = source_directories
        self._lock = threading.Lock()

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
            # Size and mtime only: snapshot copies hardlink jackets, which changes ctime
            # and would invalidate every preview URL the browser already holds.
            identity = (stat.st_size, stat.st_mtime_ns)
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
        path = self.directory / f"{song_id}-{revision}.webp"
        with self._lock:
            try:
                return path.read_bytes()
            except FileNotFoundError:
                pass
            content = self._render(source)
            # Older revisions of this jacket are never requested again.
            for stale in self.directory.glob(f"{song_id}-*.webp"):
                stale.unlink(missing_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".thumb-", suffix=".tmp", dir=self.directory
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return content
