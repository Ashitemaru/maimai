"""Filtering, sorting and aggregation over the shared read-only chart rows."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Query
from pydantic import Field

from scripts.jp_versions import JP_VERSION_NAMES

from .store import normalized

ChartSort = Literal[
    "constant_desc",
    "constant_asc",
    "achievement_desc",
    "achievement_asc",
    "ap_desc",
    "ap_asc",
    "title",
]
CNVersion = Annotated[int, Field(ge=10000)] | Literal["__unknown__"]
COMPLETION_FIELDS = (
    "id",
    "difficulty",
    "chart_key",
    "title",
    "display_title",
    "type",
    "difficulty_name",
    "level",
    "constant",
    "jacket_url",
    "is_complete",
    "is_ap",
    "status",
    "conflict",
    "provider_achievement",
    "pending_record",
    "record_revision",
    "venue_pending",
)
VERSION_LABELS = {"DX": "でらっくす", "DX PLUS": "でらっくす PLUS", "__unknown__": "版本待核实"}


def chart_filters(
    q: str = "",
    group: Literal["all", "low", "high", "catalog"] = "all",
    difficulty: int | None = Query(default=None, ge=0, le=4),
    type: Literal["standard", "dx"] | None = None,
    status: Literal["all", "complete", "incomplete", "ap", "legacy", "conflict"] = "all",
    constant_min: float | None = None,
    constant_max: float | None = None,
    level: str | None = None,
    venue: str | None = None,
    version: str | None = None,
    cn_version: CNVersion | None = Query(default=None),
):
    return {
        "q": q,
        "group": group,
        "difficulty": difficulty,
        "type": type,
        "status": status,
        "constant_min": constant_min,
        "constant_max": constant_max,
        "level": level,
        "venue": venue,
        "version": version,
        "cn_version": cn_version,
    }


def chart_version(chart):
    return chart.get("jp_release_version") or "__unknown__"


def level_order(level):
    return float(level.rstrip("+")) + (0.5 if "+" in level else 0)


def sort_charts(items, sort: ChartSort):
    def stable_key(c):
        return (-c["constant"], int(c["song_id"]), c["type"], c["difficulty_index"], c["chart_key"])

    if sort == "title":

        def key(c):
            return (c["title"], c["difficulty_index"], c["type"])
    elif sort in ("achievement_desc", "achievement_asc"):

        def key(c):
            value = c.get("provider_achievement")
            if value is None:
                return (True, 0, *stable_key(c))
            return (False, value if sort == "achievement_asc" else -value, *stable_key(c))
    elif sort in ("ap_desc", "ap_asc"):

        def key(c):
            return ((-1 if sort == "ap_desc" else 1) * int(c["is_ap"]), *stable_key(c))
    else:

        def key(c):
            return (
                (-1 if sort == "constant_desc" else 1) * c["constant"],
                int(c["song_id"]),
                c["type"],
                c["difficulty_index"],
                c["chart_key"],
            )

    return sorted(items, key=key)


def filtered(
    items,
    waterfish_map,
    *,
    q="",
    group="all",
    difficulty=None,
    type=None,
    status="all",
    constant_min=None,
    constant_max=None,
    level=None,
    venue=None,
    version=None,
    cn_version=None,
):
    needle = normalized(q)
    ext = waterfish_map.get(needle)

    def matches(c):
        if group not in ("all", "catalog") and c["group"] != group:
            return False
        if needle:
            numeric_match = needle == str(c["song_id"]) or needle == str(c["divingfish_id"])
            if ext and int(needle) >= 10000:
                numeric_match = c["song_id"] == ext["song_id"] and c["type"] == ext["type"]
            if needle not in c["_search"] and not numeric_match:
                return False
        if difficulty is not None and c["difficulty_index"] != difficulty:
            return False
        if type and c["type"] != type:
            return False
        if constant_min is not None and c["constant"] < constant_min:
            return False
        if constant_max is not None and c["constant"] > constant_max:
            return False
        if level and c["level"] != level:
            return False
        if venue and venue not in c["venue"]:
            return False
        if version and chart_version(c) != version:
            return False
        if cn_version is not None and (c.get("_cn_version_group") or "__unknown__") != cn_version:
            return False
        return {
            "all": True,
            "complete": c["is_complete"],
            "incomplete": not c["is_complete"],
            "ap": c["is_ap"],
            "legacy": c["status"] == "legacy",
            "conflict": c["conflict"],
        }[status]

    return [c for c in items if matches(c)]


def _counts():
    return dict.fromkeys(
        ("total", "complete", "ap", "remaining", "conflicts", "legacy_complete"), 0
    )


def selection_summary(items, sync):
    """Totals plus per-level and per-constant counts in one pass."""
    result, levels, constants = _counts(), {}, {}
    for c in items:
        for counts in (
            result,
            levels.setdefault(c["level"], _counts()),
            constants.setdefault(c["constant"], _counts()),
        ):
            counts["total"] += 1
            counts["complete"] += c["is_complete"]
            counts["ap"] += c["is_ap"]
            counts["remaining"] += not c["is_complete"]
            counts["conflicts"] += c["conflict"]
            counts["legacy_complete"] += c["legacy_complete"]
    result["levels"] = [
        {"level": level, **levels[level]} for level in sorted(levels, key=level_order)
    ]
    result["constants"] = [{"constant": value, **constants[value]} for value in sorted(constants)]
    result["sync"] = sync
    return result


def filter_options(charts, waterfish_map, group):
    items = filtered(charts, waterfish_map, group=group)
    positions = {name: index for index, name in enumerate(JP_VERSION_NAMES)}
    versions = sorted(
        {chart_version(c) for c in items},
        key=lambda name: (
            name == "__unknown__",
            positions.get(name, len(JP_VERSION_NAMES)),
            name,
        ),
    )
    return {
        "levels": sorted({c["level"] for c in items}, key=level_order),
        "constants": sorted({c["constant"] for c in items}),
        "versions": [
            {"value": version, "label": VERSION_LABELS.get(version, version)}
            for version in versions
        ],
        "cn_versions": getattr(charts, "cn_versions", []),
        "cn_matrix_versions": getattr(charts, "cn_matrix_versions", []),
    }


def completion_matrix(charts, waterfish_map, *, difficulty, constant_min, constant_max, cn_range):
    cn_version_min, cn_version_max = cn_range
    items = filtered(
        charts,
        waterfish_map,
        difficulty=difficulty,
        constant_min=constant_min,
        constant_max=constant_max,
    )
    versions = [
        dict(version)
        for version in getattr(charts, "cn_matrix_versions", [])
        if (cn_version_min is None or int(version["value"]) >= cn_version_min)
        and (cn_version_max is None or int(version["value"]) <= cn_version_max)
    ]
    # An unknown CN release cannot be placed inside a known release range.
    if (
        cn_version_min is None
        and cn_version_max is None
        and any(c.get("_cn_version_group") is None for c in items)
    ):
        versions.append({"value": "__unknown__", "label": "未知", "title": "国服版本待核实"})

    def empty_count():
        return {"complete": 0, "total": 0, "remaining": 0}

    totals = {version["value"]: empty_count() for version in versions}
    summary = empty_count()
    rows = {}
    for chart in items:
        version = str(chart.get("_cn_version_group") or "__unknown__")
        if version not in totals:
            continue
        constant = chart["constant"]
        if constant not in rows:
            rows[constant] = {
                "constant": constant,
                "cells": {key: empty_count() for key in totals},
                "total": empty_count(),
            }
        row = rows[constant]
        complete = int(bool(chart["is_complete"]))
        for counts in (row["cells"][version], row["total"], totals[version], summary):
            counts["complete"] += complete
            counts["total"] += 1
            counts["remaining"] += 1 - complete
    return {
        "versions": versions,
        "rows": [rows[value] for value in sorted(rows, reverse=True)],
        "totals": totals,
        "summary": summary,
    }
