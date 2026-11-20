"""Personal data downloads: venue records, venue locations and the cover asset pack."""

from __future__ import annotations

import csv
import io
import os
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from .venue_locations import venue_map


def csv_response(filename, header, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        "﻿" + output.getvalue(),  # BOM so Excel reads UTF-8.
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def venue_rows(store):
    rows = {}
    with store.db() as conn:
        conn.execute("BEGIN")
        for row in conn.execute("SELECT chart_key,venue FROM legacy WHERE complete AND venue!=''"):
            rows[row["chart_key"]] = (row["venue"], "import", "")
        for row in conn.execute("SELECT chart_key,venue,recorded_at FROM ap_confirmations"):
            rows[row["chart_key"]] = (row["venue"], "app", row["recorded_at"])
    catalog = store.catalog
    known = getattr(catalog, "source_charts", catalog)
    for key in sorted(rows, key=lambda key: tuple(int(part) for part in key.split(":"))):
        chart = catalog.get(key) or known.get(key) or {}
        venue, source, recorded_at = rows[key]
        if venue:
            yield (
                key,
                chart.get("title", ""),
                chart.get("type", ""),
                chart.get("difficulty_name", ""),
                venue,
                source,
                recorded_at,
            )


def export_router(store, assets):
    router = APIRouter(prefix="/api/export")

    @router.get("/venues.csv")
    def venues_csv():
        header = ("chart_key", "title", "type", "difficulty", "venue", "source", "recorded_at")
        return csv_response("mai-journal-venues.csv", header, venue_rows(store))

    @router.get("/venue-locations.csv")
    def venue_locations_csv():
        rows = [
            (
                item["name"],
                item["city"],
                item["location"]["latitude"] if item["location"] else "",
                item["location"]["longitude"] if item["location"] else "",
                item["location"]["updated_at"] if item["location"] else "",
            )
            for item in venue_map(store)["items"]
        ]
        header = ("venue", "city", "latitude", "longitude", "updated_at")
        return csv_response("mai-journal-venue-locations.csv", header, rows)

    @router.get("/assets.zip")
    def assets_zip():
        # Built in a temporary file (images are already compressed) and deleted after sending.
        descriptor, name = tempfile.mkstemp(prefix=".assets-", suffix=".zip", dir=store.runtime)
        try:
            with os.fdopen(descriptor, "wb") as handle, zipfile.ZipFile(handle, "w") as archive:
                for path in sorted(Path(assets).rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        archive.write(path, path.relative_to(assets).as_posix())
        except BaseException:
            os.unlink(name)
            raise
        return FileResponse(
            name,
            media_type="application/zip",
            filename="mai-journal-assets.zip",
            background=BackgroundTask(os.unlink, name),
        )

    return router
