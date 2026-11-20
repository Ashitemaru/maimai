"""Recheck a built catalog directory against its receipts before publication."""

from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from pathlib import Path

from .catalog_chart_compiler import SQL_METADATA, calculate, digest, read, sql_text
from .catalog_pipeline import check_source, merge_and_diff
from .catalog_policy import source_policy
from .catalog_record_review import _notes


def require(condition, message):
    if not condition:
        raise ValueError("Catalog validation failed: " + message)


def validate(directory):
    """Recompute the admission boundary and every permanent exception from receipts."""
    try:
        return _validate(Path(directory))
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        # Malformed generated files fail validation like any other mismatch.
        raise ValueError(
            f"Catalog validation failed: malformed catalog data ({error!r})"
        ) from error


def _validate(directory):
    catalog = read(directory / "catalog.json")
    report = read(directory / "build_report.json")
    require(isinstance(catalog, dict), "catalog.json is missing")
    require(isinstance(report, dict), "build_report.json is missing")
    require(
        catalog.get("schema_version") == 2 and "songs" not in catalog, "schema 2 flat chart catalog"
    )
    require(
        catalog.get("primary_source") == "lxns" and catalog.get("data_region") == "CN",
        "metadata authority",
    )
    require(
        catalog.get("source_policy") == source_policy() == report.get("source_policy"),
        "source policy",
    )
    for receipt in read(directory / "raw/sources.json", []):
        path = directory / "raw" / receipt["file"]
        require(
            path.is_file()
            and digest(path) == receipt["sha256"]
            and path.stat().st_size == receipt["bytes"],
            "raw source hash: " + receipt["file"],
        )
        check_source(
            receipt["file"],
            read(path),
            allow_empty=receipt.get("fetch_status") in ("stale", "unavailable"),
        )
    for receipt in catalog.get("input_files", []):
        path = directory / receipt["path"]
        require(
            path.is_file()
            and digest(path) == receipt["sha256"]
            and path.stat().st_size == receipt["bytes"],
            "input hash: " + receipt["path"],
        )
    for receipt in report.get("outputs", []):
        path = directory / receipt["path"]
        require(
            path.is_file()
            and digest(path) == receipt["sha256"]
            and path.stat().st_size == receipt["bytes"],
            "output hash: " + receipt["path"],
        )
    if (directory / "effective_lxns_songs.json").exists():
        effective, _ = merge_and_diff(
            read(directory / "raw/lxns_songs.json"),
            read(directory / "retained_lxns_records.json"),
            read(directory / "pipeline_report.json")["started_at"],
        )
        require(
            effective == read(directory / "effective_lxns_songs.json"),
            "effective raw/retained projection",
        )
    calculated = calculate(directory)
    require(
        catalog["charts"] == calculated["charts"],
        "admitted charts differ from source/decisions or pending chart leaked",
    )
    require(
        read(directory / "catalog_record_review.json") == calculated["review"],
        "pending review differs from source/decisions",
    )
    require(
        read(directory / "chart_key_map.json") == calculated["mappings"],
        "canonical chart identity mapping",
    )
    require(
        read(directory / "pending_charts.json")
        == {
            "schema_version": 2,
            "counts": calculated["review"]["counts"],
            "records": [r for r in calculated["review"]["records"] if r["status"] == "pending"],
        },
        "pending chart artifact",
    )
    require(
        catalog["decision_revision"]
        == report["decision_revision"]
        == calculated["document"].get("revision", 0),
        "decision revision",
    )
    for key, count in calculated["review"]["counts"].items():
        require(catalog["counts"][key] == count == report["counts"][key], "admission count " + key)
    require(catalog["counts"]["charts"] == len(catalog["charts"]), "formal chart count")
    require(
        catalog["versions_cn"] == calculated["lxns"]["versions"]
        and catalog["genres"] == calculated["lxns"]["genres"],
        "LXNS version/genre metadata",
    )
    by_key = {c["chart_key"]: c for c in catalog["charts"]}
    require(len(by_key) == len(catalog["charts"]), "unique chart keys")
    for chart in catalog["charts"]:
        key = chart["chart_key"]
        require(key == f"{chart['id']}:{chart['difficulty']}", "canonical key " + key)
        require(chart["cn_version"] == chart["api_version"], "chart CN version " + key)
        require(
            "level_value" not in chart and "version" not in chart,
            "legacy raw business aliases " + key,
        )
        require(chart["admission_status"] == "admitted", "pending formal chart " + key)
        require(
            all(isinstance(chart[f], str) for f in ("title", "artist", "genre", "level")),
            "chart text fields " + key,
        )
        require(
            type(chart["bpm"]) in (int, float) and math.isfinite(chart["bpm"]), "chart BPM " + key
        )
        require(
            chart["constant"] is None
            or type(chart["constant"]) in (int, float)
            and math.isfinite(chart["constant"]),
            "chart constant " + key,
        )
        require(_notes(chart["notes"], chart["type"]) is not None, "chart notes " + key)
    # All source jackets remain validated even when their charts await admission.
    from PIL import Image

    jacket_report = read(directory / "jacket_manifest.json")
    require(
        isinstance(jacket_report, dict) and isinstance(jacket_report.get("jackets"), list),
        "jacket_manifest.json is missing",
    )
    jackets = {r["song_id"]: r for r in jacket_report["jackets"]}
    require(
        len(jackets) == len(jacket_report["jackets"]) == len(calculated["lxns"]["songs"]),
        "jacket coverage",
    )
    decoded = 0
    for song in calculated["lxns"]["songs"]:
        row = jackets[song["id"]]
        require(
            row["file"] == f"jackets/{song['id']}.png" and row["asset_id"] == song["id"] % 10000,
            "jacket resource mapping",
        )
        if row["status"] == "ok":
            path = directory / row["file"]
            require(
                path.is_file()
                and digest(path) == row["sha256"]
                and path.stat().st_size == row["bytes"],
                "jacket hash",
            )
            try:
                with Image.open(path) as image:
                    valid = image.format == "PNG" and image.size == (row["width"], row["height"])
                    image.load()
            except OSError:
                valid = False
            require(valid, "jacket image format")
            decoded += 1
    for name in ("remy_release_overrides.json", "remy_conflicts.json"):
        evidence = read(directory / name, {})
        rows = (
            evidence.get("overrides", [])
            if name.endswith("overrides.json")
            else [c for r in evidence.get("conflicts", []) for c in r.get("candidates", [])]
        )
        for row in rows:
            if row.get("evidence_file") and row.get("evidence_sha256"):
                path = directory / row["evidence_file"]
                require(
                    path.is_file() and digest(path) == row["evidence_sha256"],
                    "Remy page evidence hash",
                )
    with closing(sqlite3.connect(f"file:{directory / 'catalog.sqlite'}?mode=ro", uri=True)) as conn:
        require(conn.execute("PRAGMA integrity_check").fetchone() == ("ok",), "SQLite integrity")
        rows = conn.execute("SELECT id,difficulty,type,data_json FROM charts ORDER BY rowid")
        require(
            rows.fetchall()
            == [(c["id"], c["difficulty"], c["type"], sql_text(c)) for c in catalog["charts"]],
            "SQL charts differ from catalog.json",
        )
        require(
            dict(conn.execute("SELECT key,value_json FROM metadata"))
            == {key: sql_text(catalog[key]) for key in SQL_METADATA},
            "SQL metadata",
        )
    return {
        "schema_version": 2,
        "status": "ok",
        "charts_checked": len(by_key),
        "total_charts": calculated["review"]["counts"]["total"],
        "pending_charts": calculated["review"]["counts"]["pending"],
        "images_decoded_and_hash_checked": decoded,
        "sqlite_integrity": "ok",
    }
