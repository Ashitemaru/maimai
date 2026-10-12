"""Current-level AP totals for the user's named venues."""

import re

VENUE_LEVELS = ("12+", "13", "13+", "14", "14+/15")


def summarize_venues(charts, names=(), pending_records=()):
    rows = {}

    def row(name):
        if name not in rows:
            match = re.match(r"^【([^】]+)】", name)
            rows[name] = {
                "name": name,
                "city": match[1] if match else "未分类",
                "counts": dict.fromkeys((*VENUE_LEVELS, "other"), 0),
                "total": 0,
                "pending_count": 0,
            }
        return rows[name]

    for name in names:
        if name:
            row(name)
    for chart in charts:
        name = chart.get("venue")
        if name:
            row(name)
        if name and chart.get("is_ap"):
            level = chart.get("level")
            bucket = "14+/15" if level in ("14+", "15", "15+") else level
            if bucket not in VENUE_LEVELS:
                bucket = "other"
            item = row(name)
            item["counts"][bucket] += 1
            item["total"] += 1
    for pending in pending_records:
        if pending.get("venue"):
            row(pending["venue"])["pending_count"] += 1

    levels = [*VENUE_LEVELS]
    if any(item["counts"]["other"] for item in rows.values()):
        levels.append("other")
    cities = {}
    for item in rows.values():
        cities[item["city"]] = cities.get(item["city"], 0) + item["total"]
    items = sorted(
        rows.values(),
        key=lambda item: (-cities[item["city"]], item["city"], -item["total"], item["name"]),
    )
    return {
        "levels": [
            {"value": level, "label": "其他" if level == "other" else level} for level in levels
        ],
        "items": items,
        "totals": {level: sum(item["counts"][level] for item in items) for level in levels},
        "total": sum(item["total"] for item in items),
        "pending_count": sum(item["pending_count"] for item in items),
    }
