#!/usr/bin/env python3
"""Fetch, classify, validate, and merge the GaryShare public M3U source."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from validate_playlist import Entry, parse_playlist, probe_entry, write_playlist, write_txt_playlist
except ModuleNotFoundError:
    from scripts.validate_playlist import Entry, parse_playlist, probe_entry, write_playlist, write_txt_playlist
from utils.tools import sanitize_filename_from_url


DEFAULT_SOURCE_URL = "https://garyshare.sharewithyou.dpdns.org/mylist.m3u"

GROUP_ORDER = [
    "📺央视频道",
    "📡卫视频道",
    "🇭🇰港澳台频道",
    "📰新闻财经",
    "🏀体育频道",
    "🎬影视剧场",
    "🧒少儿动漫",
    "📚纪录科教",
    "🌍欧美频道",
    "🏙️地方频道",
    "📺其他频道",
]

PAID_OR_RESTRICTED_KEYWORDS = (
    "premium",
    "hbo",
    "cinemax",
    "showtime",
    "starz",
    "sky cinema",
    "pay per view",
    "ppv",
)


@dataclass(frozen=True)
class SourceEntry:
    name: str
    url: str
    source_group: str
    tvg_id: str = ""
    tvg_logo: str = ""
    options: tuple[str, ...] = ()


def parse_attrs(text: str) -> dict[str, str]:
    return {key.lower(): value for key, value in re.findall(r'([\w-]+)="([^"]*)"', text)}


def parse_remote_m3u(content: str) -> list[SourceEntry]:
    lines = content.splitlines()
    entries: list[SourceEntry] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line.startswith("#EXTINF:"):
            index += 1
            continue

        attrs = parse_attrs(line)
        fallback_name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
        name = (attrs.get("tvg-name") or fallback_name).strip()
        options: list[str] = []
        url = ""
        index += 1
        while index < len(lines):
            candidate = lines[index].strip()
            if not candidate:
                index += 1
                continue
            if candidate.startswith("#EXTVLCOPT:"):
                options.append(candidate)
                index += 1
                continue
            if candidate.startswith("#EXTINF:"):
                break
            if not candidate.startswith("#"):
                url = candidate
                index += 1
                break
            index += 1

        if name and url:
            entries.append(
                SourceEntry(
                    name=name,
                    url=url,
                    source_group=attrs.get("group-title", "").strip(),
                    tvg_id=attrs.get("tvg-id", "").strip(),
                    tvg_logo=attrs.get("tvg-logo", "").strip(),
                    options=tuple(options),
                )
            )
    return entries


def classify_group(name: str, source_group: str) -> str:
    text = f"{name} {source_group}".lower()
    chinese_text = f"{name} {source_group}"

    if any(token in chinese_text for token in ("CCTV", "央视", "CGTN")):
        return "📺央视频道"
    if any(token in chinese_text for token in ("湖南卫视", "浙江卫视", "东方卫视", "江苏卫视", "北京卫视", "广东卫视", "深圳卫视")):
        return "📡卫视频道"
    if any(token in chinese_text for token in ("翡翠", "明珠", "TVB", "凤凰", "港台", "澳门", "台湾", "Taiwan", "Phoenix")):
        return "🇭🇰港澳台频道"
    if any(token in text for token in ("sport", "fifa", "nba", "nfl", "tennis", "golf", "football", "basketball", "赛事", "足球", "篮球", "体育")):
        return "🏀体育频道"
    if any(token in text for token in ("news", "cnn", "bbc", "al jazeera", "bloomberg", "cnbc", "cheddar", "finance", "财经", "证券", "经济", "新闻")):
        return "📰新闻财经"
    if any(token in text for token in ("kids", "cartoon", "nick", "baby", "toon", "children", "少儿", "动漫", "卡通", "儿童")):
        return "🧒少儿动漫"
    if any(token in text for token in ("docs", "documentary", "discovery", "history", "national geographic", "nasa", "nature", "纪录", "探索", "科教", "自然", "历史")):
        return "📚纪录科教"
    if any(token in text for token in ("movie", "movies", "film", "cinema", "drama", "series", "影视", "影院", "剧场", "电视剧", "电影")):
        return "🎬影视剧场"
    if source_group in {"US", "UK", "CA", "IE", "DE", "FR", "IT", "ES", "PT", "NL", "PL"}:
        return "🌍欧美频道"
    if any(token in text for token in ("fast", "lifestyle", "music", "4k uhd")):
        return "🌍欧美频道"
    return "📺其他频道"


def looks_restricted(entry: SourceEntry) -> tuple[bool, str]:
    text = f"{entry.name} {entry.source_group}".lower()
    if any(keyword in text for keyword in PAID_OR_RESTRICTED_KEYWORDS):
        return True, "paid_or_restricted_channel_keyword"

    parsed = urlsplit(entry.url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return True, f"unsupported_protocol:{scheme or 'missing'}"

    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and path_parts[-1].lower().endswith(".mp4"):
        return True, "vod_file_not_live_playlist"

    if len(path_parts) >= 3:
        first, second, third = path_parts[0], path_parts[1], path_parts[2]
        tokenish = re.fullmatch(r"[A-Za-z0-9._-]{4,}", first) and re.fullmatch(r"[A-Za-z0-9._-]{4,}", second)
        numeric_or_ts = re.fullmatch(r"\d+(\.ts)?", third) is not None
        if tokenish and numeric_or_ts and first.lower() not in {"hls", "playlist", "linear"}:
            return True, "account_style_url_path"
    if len(path_parts) >= 4 and path_parts[0].lower() == "live":
        user_part, pass_part, stream_part = path_parts[1], path_parts[2], path_parts[3]
        tokenish = re.fullmatch(r"[A-Za-z0-9._-]{4,}", user_part) and re.fullmatch(r"[A-Za-z0-9._-]{4,}", pass_part)
        numeric_or_ts = re.fullmatch(r"\d+(\.ts)?", stream_part) is not None
        if tokenish and numeric_or_ts:
            return True, "account_style_url_path"

    return False, ""


def log_url(url: str, reason: str) -> str:
    account_path = re.search(r"/[A-Za-z0-9._-]{4,}/[A-Za-z0-9._-]{4,}/\d+", url)
    if reason == "account_style_url_path" or account_path:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}/<redacted-account-path>"
    return url


def escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "'")


def build_metadata(entry: SourceEntry, group: str) -> str:
    tvg_id = escape_attr(entry.tvg_id or entry.name)
    tvg_name = escape_attr(entry.name)
    tvg_logo = escape_attr(entry.tvg_logo)
    group_attr = escape_attr(group)
    return (
        f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_name}" '
        f'tvg-logo="{tvg_logo}" group-title="{group_attr}",{entry.name}'
    )


def read_cached_source(url: str, cache_dir: Path) -> str | None:
    cache_file = cache_dir / f"{sanitize_filename_from_url(url)}.txt"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8-sig", errors="replace")
    return None


def fetch_source(url: str, timeout: int, retries: int, cache_dir: Path | None) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 iptv-api playlist validator"},
            )
            response.raise_for_status()
            return response.content.decode("utf-8-sig", errors="replace")
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(10, attempt * 2))
    if cache_dir:
        cached = read_cached_source(url, cache_dir)
        if cached:
            print(f"Using cached source content from {cache_dir} after fetch failure: {last_error}")
            return cached
    raise RuntimeError(f"failed to fetch source after {retries} attempts: {last_error}")


def validate_entries(entries: list[Entry], timeout: int, workers: int) -> tuple[list[Entry], list[dict], list[dict]]:
    valid: list[Entry] = []
    invalid: list[dict] = []
    valid_details: list[dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(probe_entry, entry, timeout) for entry in entries]
        for future in concurrent.futures.as_completed(futures):
            entry, ok, error, details = future.result()
            if ok:
                valid.append(entry)
                valid_details.append({"channel": entry.channel_name, "url": entry.url, **details})
            else:
                invalid.append({"channel": entry.channel_name, "url": entry.url, "error": error})

    order = {entry.url: index for index, entry in enumerate(entries)}
    valid.sort(key=lambda item: order.get(item.url, 10**9))
    return valid, invalid, valid_details


def group_entries(entries: list[Entry]) -> list[Entry]:
    grouped: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries:
        match = re.search(r'group-title="([^"]*)"', entry.metadata)
        group = match.group(1).strip() if match else "📺其他频道"
        grouped[group].append(entry)

    ordered: list[Entry] = []
    for group in GROUP_ORDER:
        ordered.extend(grouped.pop(group, []))
    for group in sorted(grouped):
        ordered.extend(grouped[group])
    return ordered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--playlist", type=Path, default=Path("output/user_result.m3u"))
    parser.add_argument("--txt-output", type=Path, default=Path("output/user_result.txt"))
    parser.add_argument("--report", type=Path, default=Path("output/log/garyshare_validation.json"))
    parser.add_argument("--fetch-timeout", type=int, default=30)
    parser.add_argument("--fetch-retries", type=int, default=3)
    parser.add_argument("--cache-dir", type=Path, default=Path("output/log/subscribe"))
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--min-valid", type=int, default=20)
    args = parser.parse_args()

    header, base_entries = parse_playlist(args.playlist)
    base_urls = {entry.url for entry in base_entries}

    content = fetch_source(args.source_url, args.fetch_timeout, args.fetch_retries, args.cache_dir)
    source_entries = parse_remote_m3u(content)

    seen_urls = set(base_urls)
    candidate_entries: list[Entry] = []
    skipped: list[dict] = []
    group_counts = Counter()

    for source_entry in source_entries:
        restricted, reason = looks_restricted(source_entry)
        if restricted:
            skipped.append(
                {
                    "channel": source_entry.name,
                    "url": log_url(source_entry.url, reason),
                    "source_group": source_entry.source_group,
                    "reason": reason,
                }
            )
            continue
        if source_entry.url in seen_urls:
            skipped.append(
                {
                    "channel": source_entry.name,
                    "url": source_entry.url,
                    "source_group": source_entry.source_group,
                    "reason": "duplicate_url",
                }
            )
            continue

        group = classify_group(source_entry.name, source_entry.source_group)
        group_counts[group] += 1
        seen_urls.add(source_entry.url)
        candidate_entries.append(
            Entry(
                metadata=build_metadata(source_entry, group),
                options=source_entry.options,
                url=source_entry.url,
            )
        )

    valid_new_entries, invalid_new_entries, valid_details = validate_entries(
        candidate_entries,
        timeout=args.timeout,
        workers=args.workers,
    )

    if len(valid_new_entries) < args.min_valid:
        print(
            f"Refusing to merge GaryShare source: only {len(valid_new_entries)} "
            f"valid streams, below minimum {args.min_valid}."
        )
        return 1

    merged_entries = group_entries(base_entries + valid_new_entries)
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    write_playlist(args.playlist, header, merged_entries, generated_at)
    write_txt_playlist(args.txt_output, merged_entries)

    report = {
        "generated_at": generated_at,
        "source_url": args.source_url,
        "source_entries": len(source_entries),
        "base_entries": len(base_entries),
        "candidate_entries": len(candidate_entries),
        "valid_new_entries": len(valid_new_entries),
        "invalid_new_entries": len(invalid_new_entries),
        "skipped_entries": len(skipped),
        "merged_entries": len(merged_entries),
        "candidate_group_counts": dict(group_counts),
        "skipped": skipped,
        "invalid_entries": invalid_new_entries,
        "valid_streams": valid_details,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=args.report.parent) as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
        tmp_path = Path(f.name)
    os.replace(tmp_path, args.report)

    print(
        f"GaryShare merged: {len(valid_new_entries)}/{len(candidate_entries)} "
        f"validated new streams, {len(skipped)} skipped, {len(merged_entries)} total entries."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
