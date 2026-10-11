#!/usr/bin/env python3
"""Download the saved LXNS catalog's jacket assets with pacing and resumability."""

import argparse
import hashlib
import io
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / "data" / "catalog"
BASE = "https://assets2.lxns.net/maimai/jacket/"
RESOURCE_MAPPING_SOURCE = "https://github.com/Lxns-Network/maimai-prober-frontend/blob/a581699b7bee3b642d9a7e54d678330891157049/src/utils/api/song/maimai.ts#L92-L94"
LOCK = threading.Lock()
NEXT_REQUEST = 0.0


def now():
    return datetime.now(UTC).isoformat()


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def pace(interval):
    global NEXT_REQUEST
    with LOCK:
        delay = max(0, NEXT_REQUEST - time.monotonic())
        if delay:
            time.sleep(delay)
        NEXT_REQUEST = time.monotonic() + interval


def inspect(content):
    with Image.open(io.BytesIO(content)) as im:
        im.load()
        if im.format != "PNG" or min(im.size) < 32:
            raise ValueError("Unexpected jacket format or dimensions")
        return {
            "width": im.width,
            "height": im.height,
            "format": im.format,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }


def download(song, interval):
    ident = song["id"]
    asset_id = (
        ident % 10000
    )  # Official frontend getSongResourceId; does not change the song/chart ID.
    path = ROOT / "jackets" / f"{ident}.png"
    asset_path = ROOT / "jackets" / f"{asset_id}.png"
    url = BASE + f"{asset_id}.png"
    result = {
        "song_id": ident,
        "title": song["title"],
        "url": url,
        "file": f"jackets/{ident}.png",
        "asset_id": asset_id,
    }
    if not path.exists() and asset_path.exists():
        content = asset_path.read_bytes()
        inspect(content)
        path.write_bytes(content)
    if path.exists():
        result.update(inspect(path.read_bytes()), status="ok")
        return result
    for attempt in range(4):
        pace(interval)
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "maimai-cover-local-catalog/0.1"}
            )
            with urllib.request.urlopen(request, timeout=25) as response:
                content = response.read()
                result["etag"] = response.headers.get("ETag")
            result.update(inspect(content), status="ok", fetched_at=now())
            temp = path.with_suffix(".png.part")
            temp.write_bytes(content)
            temp.replace(path)
            return result
        except urllib.error.HTTPError as error:
            result.update(
                status="missing" if error.code == 404 else "error",
                http_status=error.code,
                error=str(error),
            )
            if error.code in (400, 401, 403, 404):
                break
            retry_after = error.headers.get("Retry-After", "")
            time.sleep(float(retry_after) if retry_after.isdigit() else min(60, 5 * 2**attempt))
        except Exception as error:
            result.update(status="error", error=f"{type(error).__name__}: {error}")
            time.sleep(min(30, 2 * 2**attempt))
    result["checked_at"] = now()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--interval", type=float, default=0.35, help="Minimum seconds between network requests"
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--regular-only", action="store_true")
    args = parser.parse_args()
    if args.interval < 0.25 or not 1 <= args.workers <= 4:
        parser.error("Use interval >= 0.25 and 1..4 workers")
    (ROOT / "jackets").mkdir(exist_ok=True)
    songs = json.loads((ROOT / "raw" / "lxns_songs.json").read_text())["songs"]
    selected = [s for s in songs if not args.regular_only or s["id"] < 100000]
    manifest_path = ROOT / "jacket_manifest.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    records = {r["song_id"]: r for r in previous.get("jackets", [])}
    started = now()

    def save():
        rows = sorted(records.values(), key=lambda r: r["song_id"])
        atomic_json(
            manifest_path,
            {
                "schema_version": 1,
                "started_at": started,
                "updated_at": now(),
                "resource_mapping": {
                    "rule": "song_id % 10000 (jacket asset only)",
                    "source_url": RESOURCE_MAPPING_SOURCE,
                },
                "expected_song_count": len(songs),
                "record_count": len(rows),
                "ok_count": sum(r["status"] == "ok" for r in rows),
                "missing_or_error_count": sum(r["status"] != "ok" for r in rows),
                "total_bytes": sum(r.get("bytes", 0) for r in rows),
                "jackets": rows,
            },
        )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(download, song, args.interval): song["id"] for song in selected}
        for completed, future in enumerate(as_completed(pending), 1):
            row = future.result()
            records[row["song_id"]] = row
            if completed % 50 == 0 or completed == len(selected):
                save()
                print(
                    json.dumps(
                        {
                            "completed": completed,
                            "selected": len(selected),
                            "ok": sum(r["status"] == "ok" for r in records.values()),
                            "failed": sum(r["status"] != "ok" for r in records.values()),
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
