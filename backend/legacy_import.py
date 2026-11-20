"""One-time import of the original AP TSV tables into the personal `legacy` table."""

from __future__ import annotations

import csv
import hashlib
import json
import re

from .clock import now

DIFFICULTIES = {"BAS": 0, "ADV": 1, "EXP": 2, "MAS": 3, "ReM": 4}
SOURCE_NAMES = {
    "low": "maimai AP - （低难度）谱面表.tsv",
    "high": "maimai AP - （高难度）谱面表.tsv",
    "levels": "maimai AP - 等级完成进度.tsv",
    "summary": "maimai AP - 汇总表.tsv",
}


def import_legacy(conn, import_dir, catalog, waterfish_map, normalized):
    # The source title disambiguates Link (131) from Link(CoF) (383).
    index = {}
    for c in getattr(catalog, "source_charts", catalog).values():
        source_title = waterfish_map.get(str(c["divingfish_id"]), {}).get("source_title")
        name = source_title if source_title is not None else c["title"]
        index.setdefault((normalized(name), c["type"], c["difficulty_index"]), []).append(c)
    sources, records = {}, []
    for group, filename in SOURCE_NAMES.items():
        raw = (import_dir / filename).read_bytes()
        rows = list(csv.reader(raw.decode("utf-8-sig").splitlines(), delimiter="\t"))
        sources[group] = {
            "file": filename,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "rows": len(rows),
        }
        if group not in ("low", "high"):
            sources[group]["values"] = rows
            continue
        for col in range(0, len(rows[0]), 2):
            if not rows[0][col].strip():
                continue
            constant = float(rows[0][col])
            for rownum, row in enumerate(rows[2:], 3):
                label = row[col].strip() if col < len(row) else ""
                if not label:
                    continue
                match = re.fullmatch(r"\[(BAS|ADV|EXP|MAS|ReM),\s*(SD|DX)\]\s*(.*)", label)
                if not match:
                    raise ValueError(f"Unrecognized label {filename}:{rownum}:{col + 1}")
                diff, kind, title = match.groups()
                candidates = index.get(
                    (normalized(title), "dx" if kind == "DX" else "standard", DIFFICULTIES[diff]),
                    [],
                )
                if len(candidates) != 1:
                    raise ValueError(
                        f"Unmatched/ambiguous chart {filename}:{rownum}:{col + 1}: {label}"
                    )
                chart = candidates[0]
                if abs(chart["constant"] - constant) > 1e-6:
                    raise ValueError(f"Constant changed: {chart['chart_key']}")
                mark = row[col + 1].strip() if col + 1 < len(row) else ""
                records.append(
                    (
                        chart["chart_key"],
                        group,
                        constant,
                        bool(mark),
                        mark if group == "high" else "",
                        label,
                        filename,
                        rownum,
                        col + 1,
                    )
                )
    conn.executemany("INSERT INTO legacy VALUES(?,?,?,?,?,?,?,?,?)", records)
    report = {
        "imported_at": now(),
        "total": len(records),
        "legacy_complete": sum(r[3] for r in records),
        "groups": {
            g: {
                "total": sum(r[1] == g for r in records),
                "complete": sum(r[3] for r in records if r[1] == g),
            }
            for g in ("low", "high")
        },
        "note": "原表完成包含删除但删除前未完成；原表标记不等同已核实AP。",
        "sources": sources,
    }
    conn.execute(
        "INSERT INTO metadata VALUES('import',?)", (json.dumps(report, ensure_ascii=False),)
    )
