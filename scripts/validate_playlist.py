#!/usr/bin/env python3
"""Validate an M3U playlist with ffprobe and publish only playable HTTP streams."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Entry:
    metadata: str
    options: tuple[str, ...]
    url: str

    @property
    def channel_name(self) -> str:
        return self.metadata.rsplit(",", 1)[-1].strip()


def parse_playlist(path: Path) -> tuple[str, list[Entry]]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    header = lines[0].strip() if lines and lines[0].startswith("#EXTM3U") else "#EXTM3U"
    entries: list[Entry] = []
    metadata: str | None = None
    options: list[str] = []

    for raw_line in lines[1:]:
        line = raw_line.strip()
        if line.startswith("#EXTINF:"):
            metadata = line
            options = []
        elif metadata and line.startswith("#EXTVLCOPT:"):
            options.append(line)
        elif metadata and line and not line.startswith("#"):
            entries.append(Entry(metadata, tuple(options), line))
            metadata = None
            options = []

    return header, entries


def probe_entry(entry: Entry, timeout: int) -> tuple[Entry, bool, str, dict]:
    scheme = urlsplit(entry.url).scheme.lower()
    if scheme not in {"http", "https"}:
        return entry, False, f"unsupported protocol: {scheme or 'missing'}", {}

    command = [
        "ffprobe",
        "-v",
        "error",
        "-rw_timeout",
        str(timeout * 1_000_000),
        "-read_intervals",
        "%+3",
        "-count_packets",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,nb_read_packets",
        "-of",
        "json",
        entry.url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout + 3,
            check=False,
        )
    except FileNotFoundError:
        return entry, False, "ffprobe not found", {}
    except subprocess.TimeoutExpired:
        return entry, False, "probe timeout", {}

    if result.returncode != 0:
        error = result.stderr.strip().splitlines()
        return entry, False, error[-1][:300] if error else "ffprobe failed", {}

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return entry, False, "invalid ffprobe output", {}

    streams = payload.get("streams", [])
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not video_streams:
        return entry, False, "no video stream", {}

    video = video_streams[0]
    try:
        packet_count = int(video.get("nb_read_packets") or 0)
    except (TypeError, ValueError):
        packet_count = 0
    if packet_count <= 0:
        return entry, False, "no video packets received", {}

    details = {
        "codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "video_packets": packet_count,
    }
    return entry, True, "", details


def replace_update_time(metadata: str, generated_at: str) -> str:
    if 'group-title="🕘️更新时间"' not in metadata:
        return metadata
    return re.sub(r",.*$", f",{generated_at}", metadata)


def write_playlist(path: Path, header: str, entries: list[Entry], generated_at: str) -> None:
    output_lines = [header]
    for entry in entries:
        output_lines.append(replace_update_time(entry.metadata, generated_at))
        output_lines.extend(entry.options)
        output_lines.append(entry.url)
    content = "\n".join(output_lines) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=path.parent
    ) as temp_file:
        temp_file.write(content)
        temp_path = Path(temp_file.name)
    os.replace(temp_path, path)


def write_txt_playlist(path: Path, entries: list[Entry]) -> None:
    output_lines: list[str] = []
    current_group: str | None = None
    for entry in entries:
        group_match = re.search(r'group-title="([^"]*)"', entry.metadata)
        group = group_match.group(1).strip() if group_match else "其他频道"
        if group != current_group:
            if output_lines:
                output_lines.append("")
            output_lines.append(f"{group},#genre#")
            current_group = group
        output_lines.append(f"{entry.channel_name},{entry.url}")

    content = "\n".join(output_lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=path.parent
    ) as temp_file:
        temp_file.write(content)
        temp_path = Path(temp_file.name)
    os.replace(temp_path, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("playlist", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--txt-output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--min-channels", type=int, default=10)
    args = parser.parse_args()

    output_path = args.output or args.playlist
    header, entries = parse_playlist(args.playlist)
    unique_entries: list[Entry] = []
    seen_urls: set[str] = set()
    for entry in entries:
        if entry.url not in seen_urls:
            seen_urls.add(entry.url)
            unique_entries.append(entry)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(probe_entry, entry, args.timeout) for entry in unique_entries]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    result_by_url = {entry.url: (valid, error, details) for entry, valid, error, details in results}
    valid_entries = [
        entry for entry in unique_entries if result_by_url.get(entry.url, (False, "", {}))[0]
    ]
    channel_count = len(
        {
            entry.channel_name
            for entry in valid_entries
            if 'group-title="🕘️更新时间"' not in entry.metadata
        }
    )
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    report = {
        "generated_at": generated_at,
        "input_entries": len(entries),
        "unique_urls": len(unique_entries),
        "valid_entries": len(valid_entries),
        "valid_channels": channel_count,
        "invalid_entries": [
            {"channel": entry.channel_name, "url": entry.url, "error": error}
            for entry, valid, error, _ in results
            if not valid
        ],
        "valid_streams": [
            {
                "channel": entry.channel_name,
                "url": entry.url,
                **result_by_url[entry.url][2],
            }
            for entry in valid_entries
        ],
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    print(
        f"Validated {len(valid_entries)}/{len(unique_entries)} unique streams "
        f"across {channel_count} channels."
    )
    if channel_count < args.min_channels:
        print(
            f"Refusing to publish: {channel_count} valid channels is below "
            f"the required minimum of {args.min_channels}."
        )
        return 1

    write_playlist(output_path, header, valid_entries, generated_at)
    if args.txt_output:
        write_txt_playlist(args.txt_output, valid_entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
