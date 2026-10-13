"""Deterministic, chart-scoped collection ordering. No account-specific video table."""

from __future__ import annotations

import copy
import re
import unicodedata

from scripts.build_catalog import JP_VERSION_BOUNDARIES

from .bilibili_client import BVID, BilibiliError, digest

VERSION_RANK = {version: i for i, (_, version) in enumerate(JP_VERSION_BOUNDARIES)}
TITLE = re.compile(
    r"^【maimai】(?P<song>.+?) (?P<difficulty>Basic|Advanced|Expert|Master|Re:Master|Utage)(?P<constant>\d{1,2}(?:\.\d|\+?\?)) AP\+?$"
)
DIFFICULTY = {
    name: index for index, name in enumerate(("Basic", "Advanced", "Expert", "Master", "Re:Master"))
}
MIRROR_BVID = "BV1bJouBSEiH"


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def parsed_title(title, bvid):
    if bvid == MIRROR_BVID and title.endswith("【iamiam】"):
        title = title[::-1].replace("】maimai【", "【maimai】", 1)
    match = TITLE.fullmatch(title)
    if match is None:
        return None
    song = match["song"]
    marker = re.search(r"\[(STD|DX)\]$", song)
    kind = None
    if marker:
        kind = "standard" if marker[1] == "STD" else "dx"
        song = song[: marker.start()].strip()
    if match["difficulty"] == "Utage":
        kind = "utage"
    return {
        "song": normalized(song),
        "difficulty": DIFFICULTY.get(match["difficulty"]),
        "type": kind,
    }


def chart_identity(chart):
    return {
        "song": normalized(chart.get("display_title") or chart["title"]),
        "difficulty": chart.get("difficulty_index"),
        "type": chart["type"],
    }


def section_snapshot(data, *, section_id, season_id):
    header = data.get("section")
    if not isinstance(header, dict) or not all(
        key in header for key in ("id", "seasonId", "title", "type")
    ):
        raise BilibiliError("B 站没有返回完整小节信息，无法核对排序", status=409)
    if (
        header["id"] != section_id
        or header["seasonId"] != season_id
        or type(header["type"]) is not int
        or header["type"] not in (0, 1)
        or not isinstance(header["title"], str)
    ):
        raise BilibiliError("B 站小节身份已变化，请刷新合集", status=409)
    episodes = data.get("episodes")
    if episodes is None:
        episodes = []
    if not isinstance(episodes, list):
        raise BilibiliError("B 站小节稿件格式已变化", status=409)
    rows = []
    for episode in episodes:
        if not isinstance(episode, dict) or any(
            type(episode.get(key)) is not int or episode[key] <= 0 for key in ("id", "aid", "cid")
        ):
            raise BilibiliError("B 站返回了不完整的合集稿件", status=409)
        bvid = episode.get("bvid") or (episode.get("archive") or {}).get("bvid")
        if (
            not isinstance(bvid, str)
            or not BVID.fullmatch(bvid)
            or not isinstance(episode.get("title"), str)
        ):
            raise BilibiliError("B 站返回了不完整的合集稿件", status=409)
        rows.append(
            {
                "episode_id": episode["id"],
                "aid": episode["aid"],
                "cid": episode["cid"],
                "bvid": bvid,
                "title": episode["title"],
            }
        )
    if any(len({row[key] for row in rows}) != len(rows) for key in ("episode_id", "aid", "bvid")):
        raise BilibiliError("合集存在重复身份，无法安全重排", status=409)
    return {
        "section": {key: header[key] for key in ("id", "seasonId", "title", "type")},
        "episodes": rows,
    }


class ChartResolver:
    def __init__(self, charts, bindings, latest_titles):
        self.charts = {chart["chart_key"]: chart for chart in charts}
        self.bindings = bindings
        self.latest_titles = latest_titles
        self.evidence = {}

    def candidates(self, parsed):
        if parsed is None:
            return []
        result = []
        for chart in self.charts.values():
            if parsed["type"] == "utage":
                if chart["type"] != "utage":
                    continue
                aliases = {
                    normalized(re.sub(r"^\[[^\]]+\]", "", str(chart.get(key) or "")))
                    for key in ("title", "display_title")
                }
            else:
                if (
                    chart["type"] == "utage"
                    or chart.get("difficulty_index") != parsed["difficulty"]
                ):
                    continue
                aliases = {normalized(chart.get(key) or "") for key in ("title", "display_title")}
            if parsed["type"] and parsed["type"] != chart["type"]:
                continue
            if parsed["song"] in aliases:
                result.append(chart)
        return result

    def resolve(self, episode, explicit=None):
        bvid = episode["bvid"]
        observed = episode["title"]
        binding = self.bindings.get(bvid)
        parsed = parsed_title(observed, bvid)
        candidates = self.candidates(parsed)
        if explicit is not None:
            chart, source = explicit, "user"
        else:
            # Section titles can lag even when the old title has a unique match.
            latest = self.latest_titles(bvid)
            if latest is None:
                return None, "无法读取当前账号的真实稿件标题"
            observed = latest
            parsed = parsed_title(observed, bvid)
            candidates = self.candidates(parsed)
            if binding:
                chart = self.charts.get(binding["chart_key"])
                if chart is None or chart_identity(chart) != binding["chart_identity"]:
                    return None, "已绑定谱面的曲库身份发生变化"
                changed = parsed != binding["title_identity"] or (
                    parsed is None and observed != binding["observed_title"]
                )
                if changed and (
                    len(candidates) != 1 or candidates[0]["chart_key"] != chart["chart_key"]
                ):
                    return None, "稿件标题与已确认谱面不一致"
                source = binding.get("source", "user")
            elif len(candidates) == 1:
                chart, source = candidates[0], "exact_title"
            else:
                return None, "标题对应多张谱面，无法确定 STD/DX" if len(
                    candidates
                ) > 1 else "标题无法明确对应曲库谱面"
        self.evidence[bvid] = {
            "chart_key": chart["chart_key"],
            "chart_identity": chart_identity(chart),
            "title_identity": parsed_title(observed, bvid),
            "observed_title": observed,
            "source": source,
        }
        return chart, None


def order_report(snapshot, resolver, *, season_title, target=None, explicit=None):
    """Use Python's stable sort so equal version/name items retain relative order."""
    rows = copy.deepcopy(snapshot["episodes"])
    target_aid = target["aid"] if target else None
    pending = target is not None and all(row["aid"] != target_aid for row in rows)
    if pending:
        rows.append(target | {"episode_id": None})
    elif target is not None:
        for row in rows:
            if row["aid"] == target_aid:
                row["title"] = target["title"]
    items, unmatched = [], []
    for index, row in enumerate(rows):
        chart, problem = resolver.resolve(row, explicit if row["aid"] == target_aid else None)
        version = chart.get("jp_release_version") if chart else None
        if chart is not None and version not in VERSION_RANK:
            problem = "谱面的日服版本尚未确定"
        if problem and len(rows) > 1:
            unmatched.append(
                {key: row[key] for key in ("bvid", "aid", "title")} | {"reason": problem}
            )
        items.append(
            {key: row[key] for key in ("episode_id", "aid", "bvid", "title")}
            | {
                "song_title": (chart.get("display_title") or chart["title"]) if chart else None,
                "chart_key": chart["chart_key"] if chart else None,
                "version": version,
                "position": index + 1,
            }
        )
    desired = (
        []
        if unmatched
        else sorted(
            items,
            key=lambda item: (
                VERSION_RANK.get(item["version"], -1),
                normalized(item["song_title"] or item["title"]),
            ),
        )
    )
    desired = [item | {"position": index + 1} for index, item in enumerate(desired)]
    current = [item for item in items if item["episode_id"] is not None]
    before = [item["aid"] for item in items]
    after = [item["aid"] for item in desired]
    report = {
        "season_id": snapshot["section"]["seasonId"],
        "season_title": season_title,
        "section_id": snapshot["section"]["id"],
        "section_title": snapshot["section"]["title"],
        "status": "blocked" if unmatched else ("ordered" if before == after else "changes"),
        "revision": digest(snapshot),
        "current": current,
        "desired": desired,
        "unmatched": unmatched,
    }
    if target:
        report.update(
            target_bvid=target["bvid"],
            target_position=next(
                (item["position"] for item in desired if item["aid"] == target_aid), None
            ),
        )
    return report
