#!/usr/bin/env python3
"""Resolve Re:MASTER debuts using Remy evidence, without an LLM or catalog writes."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
import unicodedata
import urllib.request
import zlib
from collections import defaultdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit

try:
    from .build_catalog import JP_VERSION_BOUNDARIES
    from .remy_html import parse_version_page
except ImportError:
    from build_catalog import JP_VERSION_BOUNDARIES
    from remy_html import parse_version_page

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "data/catalog"
MAX_PAGE_BYTES = 8 * 1024 * 1024


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    path = unquote(parsed.path).removeprefix("/wiki/").lstrip("/")
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != "silentblue.remywiki.com"
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
        or "/" in path
        or not path.startswith(("maimai:", "maimai_DX:"))
    ):
        raise ValueError(f"Not a Remy maimai version page: {url}")
    return "https://silentblue.remywiki.com/" + path


def version_pages() -> list[str]:
    result = []
    for code, version in reversed(JP_VERSION_BOUNDARIES):
        if code < 15000:  # Re:MASTER first appeared in ORANGE PLUS.
            continue
        page = {"DX": "maimai_DX:1st", "DX PLUS": "maimai_DX:PLUS"}.get(version)
        if not page:
            page = ("maimai_DX:" if code >= 20000 else "maimai:") + version.replace(" ", "_")
        result.append(canonical_url("https://silentblue.remywiki.com/" + page))
    return result


def remaster_charts(lxns: dict) -> dict[str, dict]:
    result = {}
    for song in lxns["songs"]:
        for kind in ("standard", "dx"):
            for chart in song["difficulties"].get(kind, []):
                if chart["difficulty"] != 4:
                    continue
                key = f"{song['id']}:{kind}:4"
                if key in result:
                    raise ValueError(f"Duplicate LXNS chart: {key}")
                result[key] = {
                    "chart_key": key,
                    "song_id": song["id"],
                    "type": kind,
                    "difficulty": 4,
                    "title": song["title"],
                    "artist": song["artist"],
                }
    return result


def verified_remy(record: dict) -> bool:
    try:
        # Previously audited evidence can come from a song page and may predate
        # revision capture. New automatic records have stricter requirements.
        source = urlsplit(record["source_url"])
        if (
            source.scheme not in {"http", "https"}
            or source.username
            or source.password
            or source.hostname != "silentblue.remywiki.com"
        ):
            return False
        datetime.strptime(record["jp_release_date"], "%Y-%m-%d")
    except (KeyError, ValueError, TypeError):
        return False
    return (
        record.get("verified") is True
        and bool(record.get("jp_release_version"))
        and bool(record.get("evidence"))
        and record.get("source_provider", "remywiki") == "remywiki"
    )


def manual_records(records) -> dict[str, dict]:
    """Only explicit final manual values protect a chart; source hints do not."""
    if isinstance(records, dict):
        records = records.values()
    return {
        record["chart_key"]: record
        for record in records or []
        if record.get("jp_release_provider", record.get("source_provider")) == "manual"
    }


def resolve(
    lxns: dict, overrides: list[dict], pages: list[dict], *, protected_records=None
) -> dict:
    """Match against ALL Re:MASTER charts, including already verified siblings."""
    charts = remaster_charts(lxns)
    protected = {
        key: record for key, record in manual_records(protected_records).items() if key in charts
    }
    known = {}
    for record in overrides:
        if verified_remy(record):
            key = record["chart_key"]
            if key in known:
                raise ValueError(f"Duplicate Remy override: {key}")
            known[key] = record
    by_title = defaultdict(list)
    for chart in charts.values():
        by_title[normalize(chart["title"])].append(chart)
    proposals = defaultdict(list)
    issues, ambiguous, identity_conflicts = [], set(), defaultdict(list)
    checked = set()
    for page in pages:
        for issue in page.get("issues", []):
            issues.append({"source_url": page["source_url"], "reason": issue})
        if not page.get("source_revision"):
            issues.append({"source_url": page["source_url"], "reason": "missing_revision"})
            continue
        for row in page["rows"]:
            matches = by_title[normalize(row["title"])]
            if row.get("chart_type"):
                matches = [m for m in matches if m["type"] == row["chart_type"]]
            if len(matches) > 1 and row.get("artist"):
                artist_matches = [
                    m for m in matches if normalize(m["artist"]) == normalize(row["artist"])
                ]
                if artist_matches:
                    matches = artist_matches
            if len(matches) != 1:
                if matches:
                    keys = [m["chart_key"] for m in matches]
                    ambiguous.update(keys)
                    checked.update(keys)
                    issues.append(
                        {
                            "source_url": page["source_url"],
                            "title": row["title"],
                            "reason": "ambiguous_chart",
                            "candidates": keys,
                        }
                    )
                    for match in matches:
                        identity_conflicts[match["chart_key"]].append(
                            {
                                "chart_key": match["chart_key"],
                                "title": row["title"],
                                "artist": row.get("artist"),
                                "jp_release_version": row["version"],
                                "jp_release_date": row["date"],
                                "source_provider": "remywiki",
                                "source_url": page["source_url"],
                                "source_revision": page["source_revision"],
                                "evidence_row": row["evidence_row"],
                                "verified": False,
                                "match_status": "identity_conflict",
                                "match_method": "ambiguous_chart",
                                **page.get("provenance", {}),
                            }
                        )
                continue  # JP-only titles never enter the CN catalog.
            chart = matches[0]
            checked.add(chart["chart_key"])
            record = {k: v for k, v in chart.items() if k != "artist"}
            record.update(
                {
                    "jp_release_version": row["version"],
                    "jp_release_date": row["date"],
                    "source_provider": "remywiki",
                    "source_url": canonical_url(page["source_url"]),
                    "source_revision": page["source_revision"],
                    "verified": True,
                    "artist": row.get("artist"),
                    "evidence": "Japanese dated New Songs row with populated Re:MASTER column.",
                    "evidence_row": row["evidence_row"],
                    "match_method": "normalized_title_artist"
                    + ("_explicit_type" if row.get("chart_type") else "_unique_remaster"),
                }
            )
            record.update(page.get("provenance", {}))
            if not row.get("artist") or normalize(chart["artist"]) != normalize(row["artist"]):
                record.update(
                    verified=False, match_status="identity_conflict", match_method="artist_mismatch"
                )
                identity_conflicts[chart["chart_key"]].append(record)
                continue
            proposals[chart["chart_key"]].append(record)
    additions, conflicts = [], []
    for key, candidates in identity_conflicts.items():
        conflicts.append(
            {
                "chart_key": key,
                "reason": "identity_mismatch",
                "candidates": candidates,
                "current": protected.get(key, known.get(key)),
            }
        )
    for key, candidates in proposals.items():
        if key in protected:
            current = protected[key]
            # A user can deliberately leave the date unset; a sourced date is
            # not a contradiction unless the manual date was explicitly fixed.
            differs = any(
                r["jp_release_version"] != current.get("jp_release_version")
                or (
                    current.get("jp_release_date")
                    and r["jp_release_date"] != current["jp_release_date"]
                )
                for r in candidates
            )
            if differs:
                conflicts.append(
                    {
                        "chart_key": key,
                        "reason": "conflicting_debut_evidence",
                        "candidates": candidates,
                        "current": current,
                    }
                )
            continue
        compared = candidates + ([known[key]] if key in known else [])
        values = {(r["jp_release_version"], r["jp_release_date"]) for r in compared}
        if len(values) != 1:
            conflicts.append(
                {
                    "chart_key": key,
                    "reason": "conflicting_debut_evidence",
                    "candidates": compared,
                    "current": known.get(key),
                }
            )
        elif key not in known and key not in identity_conflicts:
            additions.append(candidates[0])
    conflict_keys = {c["chart_key"] for c in conflicts}
    resolved = set(known) | set(protected) | {r["chart_key"] for r in additions}
    pending = [
        dict(
            chart,
            reason="conflicting_evidence"
            if key in conflict_keys
            else "ambiguous_chart"
            if key in ambiguous
            else "no_remy_evidence",
        )
        for key, chart in charts.items()
        if key not in resolved
    ]
    return {
        "schema_version": 1,
        "status": "partial" if pending or conflicts else "complete",
        "target_count": len(charts),
        "already_verified": len(charts.keys() & known.keys()),
        "manual_protected": len(protected),
        "checked_chart_keys": sorted(checked),
        "resolved_count": len(additions),
        "additions": sorted(additions, key=lambda r: (r["song_id"], r["type"])),
        "pending": pending,
        "conflicts": conflicts,
        "issues": issues,
    }


def parse_evidence(data: bytes, source_url: str, provenance: dict) -> dict:
    page = parse_version_page(data.decode("utf-8-sig"), canonical_url(source_url))
    page["provenance"] = dict(provenance, evidence_sha256=hashlib.sha256(data).hexdigest())
    return page


def cached_pages(catalog_dir: Path, cache_dir: Path) -> tuple[list[dict], list[dict]]:
    pages, errors = [], []
    manifests = [
        (catalog_dir / "remy-research/evidence_manifest.json", catalog_dir),
        (cache_dir / "index.json", cache_dir),
    ]
    for manifest, base in manifests:
        if not manifest.exists():
            continue
        for entry in read_json(manifest)["pages"]:
            try:
                path = (base / entry["file"]).resolve()
                if not path.is_relative_to(base.resolve()):
                    raise ValueError("Evidence path leaves cache directory")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise ValueError("Evidence SHA256 mismatch")
                provenance = {
                    "evidence_file": str(path),
                    "retrieved_at": entry.get("retrieved_at"),
                    "retrieval_method": entry.get("retrieval_method", "internet_archive_snapshot"),
                }
                if entry.get("archive_timestamp"):
                    provenance.update(
                        archive_timestamp=entry["archive_timestamp"],
                        archive_url=f"https://web.archive.org/web/{entry['archive_timestamp']}/{entry['source_url']}",
                    )
                pages.append(parse_evidence(data, entry["source_url"], provenance))
            except (OSError, ValueError, KeyError) as error:
                errors.append({"source_url": entry.get("source_url"), "reason": str(error)})
    return pages, errors


class FetchError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemyFetcher:
    """Single-worker fetcher: 60s spacing, bounded requests, persistent cooldown."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        max_requests=8,
        interval=60,
        opener=None,
        now=time.time,
        sleep=time.sleep,
    ):
        self.cache_dir = cache_dir
        self.max_requests = max_requests
        self.interval = max(60, interval)
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open
        self.now, self.sleep = now, sleep
        self.requests = 0
        self.stopped = False
        self.state_path = cache_dir / "fetch_state.json"
        self.state = read_json(self.state_path) if self.state_path.exists() else {}

    def ordered_pages(self) -> list[str]:
        attempts = self.state.get("attempted_pages", {})
        return sorted(version_pages(), key=lambda url: attempts.get(url, 0))

    def cooldown(self, seconds):
        self.state["retry_after"] = self.now() + seconds
        self.stopped = True
        write_json(self.state_path, self.state)

    def fetch(self, url: str) -> dict:
        url = canonical_url(url)
        if self.stopped or self.now() < self.state.get("retry_after", 0):
            raise FetchError("remy_cooldown")
        if self.requests >= self.max_requests:
            raise FetchError("request_budget_exhausted")
        remaining = self.state.get("last_request", 0) + self.interval - self.now()
        if remaining > 0:
            self.sleep(remaining)
        self.requests += 1
        self.state["last_request"] = self.now()
        self.state.setdefault("attempted_pages", {})[url] = self.now()
        write_json(self.state_path, self.state)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "maimai-cover-remy-parser/1.0", "Accept-Encoding": "identity"},
        )
        try:
            with self.opener(request, timeout=30) as response:
                if canonical_url(response.geturl()) != url:
                    raise FetchError("unexpected_redirect")
                if "text/html" not in response.headers.get("Content-Type", "").lower():
                    raise FetchError("not_html")
                data = response.read(MAX_PAGE_BYTES + 1)
                if len(data) > MAX_PAGE_BYTES:
                    raise FetchError("page_too_large")
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                if encoding == "gzip":
                    data = gzip.decompress(data)
                elif encoding == "deflate":
                    data = zlib.decompress(data)
                elif encoding == "zstd":
                    try:
                        from compression import zstd
                    except ImportError as error:
                        raise FetchError("zstd_requires_python_3_14") from error
                    try:
                        data = zstd.decompress(data)
                    except zstd.ZstdError as error:
                        raise FetchError(f"invalid_zstd:{error}") from error
                elif encoding != "identity":
                    raise FetchError(f"unsupported_encoding:{encoding}")
                if len(data) > MAX_PAGE_BYTES:
                    raise FetchError("decoded_page_too_large")
        except HTTPError as error:
            if error.code in (403, 429):
                cooldown = 86400 if error.code == 403 else 3600
                header = error.headers.get("Retry-After", "") if error.headers else ""
                try:
                    cooldown = max(cooldown, float(header))
                except ValueError:
                    try:
                        cooldown = max(
                            cooldown, parsedate_to_datetime(header).timestamp() - self.now()
                        )
                    except (TypeError, ValueError):
                        pass
                self.cooldown(cooldown)
            error.close()
            raise FetchError(f"http_{error.code}") from error
        except (URLError, OSError, ValueError, EOFError, zlib.error) as error:
            raise FetchError(f"fetch_failed:{error}") from error
        timestamp = datetime.fromtimestamp(self.now(), UTC).isoformat()
        sha = hashlib.sha256(data).hexdigest()
        filename = f"pages/{sha}.html"
        provenance = {
            "evidence_file": str((self.cache_dir / filename).resolve()),
            "retrieved_at": timestamp,
            "retrieval_method": "direct_remy",
        }
        try:
            page = parse_evidence(data, url, provenance)
        except ValueError as error:
            raise FetchError(f"invalid_html:{error}") from error
        if "access_challenge_page" in page["issues"]:
            self.cooldown(86400)
            raise FetchError("access_challenge_page")
        if not page.get("source_revision") or (not page["rows"] and page["issues"]):
            raise FetchError("unusable_remy_page:" + ";".join(page["issues"]))
        target = self.cache_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        index_path = self.cache_dir / "index.json"
        entries = read_json(index_path)["pages"] if index_path.exists() else []
        entries = [e for e in entries if canonical_url(e["source_url"]) != url]
        entries.append(
            {
                "source_url": url,
                "file": filename,
                "sha256": sha,
                "retrieved_at": timestamp,
                "retrieval_method": "direct_remy",
            }
        )
        write_json(index_path, {"schema_version": 1, "pages": entries})
        return page


def run(
    catalog_dir: Path,
    *,
    overrides_path: Path | None = None,
    fetcher=None,
    page_urls: list[str] | None = None,
    cache_dir: Path | None = None,
    recheck: bool = False,
    protected_records=None,
) -> dict:
    effective = catalog_dir / "effective_lxns_songs.json"
    lxns = read_json(effective if effective.exists() else catalog_dir / "raw/lxns_songs.json")
    overrides = read_json(overrides_path or catalog_dir / "remy_release_overrides.json")[
        "overrides"
    ]
    if protected_records is None:
        compiled = catalog_dir / "catalog.json"
        document = read_json(compiled) if compiled.exists() else {}
        if document.get("schema_version") == 2:
            protected_records = [
                {**chart, "chart_key": chart["legacy_chart_key"]}
                for chart in document.get("charts", [])
            ]
        else:
            protected_records = [
                chart for song in document.get("songs", []) for chart in song.get("charts", [])
            ]
        # A permanently chosen white-chart version may belong to a chart still
        # awaiting other field decisions, so it need not be in the formal catalog.
        decisions_path = catalog_dir / "catalog_adjudications.json"
        if decisions_path.exists():
            legacy_keys = {
                (
                    song["id"] + 10000 if kind == "dx" else song["id"],
                    chart["difficulty"],
                ): f"{song['id']}:{kind}:{chart['difficulty']}"
                for song in lxns["songs"]
                for kind, charts in song["difficulties"].items()
                for chart in charts
            }
            for decision in read_json(decisions_path).get("decisions", []):
                if (
                    decision.get("kind") == "chart_field"
                    and decision.get("field_key") == "jp_release_version"
                    and decision.get("difficulty") == 4
                ):
                    legacy = legacy_keys.get((decision.get("id"), 4))
                    if legacy:
                        protected_records.append(
                            {
                                "chart_key": legacy,
                                "jp_release_provider": "manual",
                                "jp_release_version": decision["value"],
                                "jp_release_date": decision.get("date"),
                            }
                        )
    report = resolve(lxns, overrides, [], protected_records=protected_records)
    pages, errors = [], []
    requested_urls, fetched_urls = [], []
    if report["pending"] or recheck:
        pages, errors = cached_pages(catalog_dir, cache_dir or catalog_dir / "remy-cache")
        report = resolve(lxns, overrides, pages, protected_records=protected_records)
        if fetcher:
            requested_urls = list(
                dict.fromkeys(page_urls if page_urls is not None else fetcher.ordered_pages())
            )
            for url in requested_urls:
                if not report["pending"] and not recheck:
                    break
                try:
                    pages.append(fetcher.fetch(url))
                    fetched_urls.append(url)
                    report = resolve(lxns, overrides, pages, protected_records=protected_records)
                except FetchError as error:
                    reason = str(error)
                    errors.append({"source_url": url, "reason": reason})
                    if reason in {
                        "remy_cooldown",
                        "request_budget_exhausted",
                        "http_403",
                        "http_429",
                        "access_challenge_page",
                    }:
                        break
    report.update(
        generated_at=datetime.now(UTC).isoformat(),
        parser="remy_rules_v2",
        llm_used=False,
        page_count=len(pages),
        recheck=recheck,
        recheck_scope="cache_and_live" if fetcher else "cache_only",
        requested_page_urls=requested_urls,
        fetched_page_urls=fetched_urls,
        recheck_complete=bool(
            recheck and fetcher and requested_urls == fetched_urls and not errors
        ),
        fetch_count=fetcher.requests if fetcher else 0,
        fetch_errors=errors,
    )
    # A proposal for the future staged updater; this never modifies the running catalog.
    new_keys = {r["chart_key"] for r in report["additions"]}
    report["proposed_overrides"] = {
        "schema_version": 1,
        "overrides": [r for r in overrides if r["chart_key"] not in new_keys] + report["additions"],
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--overrides", type=Path, help="Input overrides (e.g. a replay subset)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/remy-parser")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument(
        "--fetch", action="store_true", help="Fetch unresolved pages at most once per 60 seconds"
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="Recheck existing evidence as well as missing versions; conflicts retain adopted values",
    )
    parser.add_argument(
        "--page", action="append", help="Remy version URL; repeat to control page order"
    )
    parser.add_argument("--max-requests", type=int, default=8)
    args = parser.parse_args()
    if args.max_requests < 1:
        parser.error("--max-requests must be positive")
    cache_dir = args.cache_dir or args.catalog_dir / "remy-cache"
    fetcher = RemyFetcher(cache_dir, max_requests=args.max_requests) if args.fetch else None
    report = run(
        args.catalog_dir,
        overrides_path=args.overrides,
        fetcher=fetcher,
        page_urls=args.page,
        cache_dir=cache_dir,
        recheck=args.recheck,
    )
    proposed = report.pop("proposed_overrides")
    write_json(args.output_dir / "remy_release_overrides.proposed.json", proposed)
    write_json(args.output_dir / "report.json", report)
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "status",
                    "target_count",
                    "already_verified",
                    "resolved_count",
                    "page_count",
                    "fetch_count",
                )
            },
            ensure_ascii=False,
        )
    )
    print(
        f"pending={len(report['pending'])} conflicts={len(report['conflicts'])} report={args.output_dir / 'report.json'}"
    )


if __name__ == "__main__":
    main()
