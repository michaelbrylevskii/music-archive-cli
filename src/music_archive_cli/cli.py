#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma", ".alac"}
GENERIC_DIRS = {
    "Music",
    "Needs to resort",
    "Other",
    "Other Rock",
    "VK",
    "YaMusic1",
    "YaMusic2",
    "На сортировку 1",
    "На сортировку 2",
    "Мне нравится",
    "Альбомы",
    "Albums",
}
DISC_DIR_RE = re.compile(r"^(?:cd|disc|disk|диск)\s*\d+$", re.IGNORECASE)
TRACK_PREFIX_RE = re.compile(r"^\s*(?:cd\s*)?(\d{1,3})(?:[.\s_-]+)(.+)$", re.IGNORECASE)
YEAR_ALBUM_RE = re.compile(r"^\s*((?:19|20)\d{2})\s*[-–—._ ]+\s*(.+?)\s*$")
URLISH_RE = re.compile(
    r"(?:https?://|www\.|\.ru\b|\.com\b|\.net\b|\.org\b|\.ua\b|\.info\b|\.biz\b|"
    r"zaycev|muzofon|mp3|vk\.com|vkontakte|4pda|rutracker|torrent|download)",
    re.IGNORECASE,
)
MOJIBAKE_CHARS = set("ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖ×ØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõö÷øùúûüýþÿ¸")
FINGERPRINT_ALGORITHM = "chromaprint"
FINGERPRINT_WORKERS = max(1, min(4, (os.cpu_count() or 2)))
HASH_WORKERS = max(1, min(4, (os.cpu_count() or 2)))


@dataclass
class Chosen:
    value: str
    source: str


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "; ".join(clean_text(v) for v in value if clean_text(v))
    value = str(value)
    value = value.replace("\x00", " ").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def is_sane(value: str) -> bool:
    value = clean_text(value)
    if not value:
        return False
    if looks_mojibake(value):
        return False
    if URLISH_RE.search(value):
        return False
    if len(value) > 180:
        return False
    return True


def looks_mojibake(value: str) -> bool:
    value = clean_text(value)
    mojibake_count = sum(1 for ch in value if ch in MOJIBAKE_CHARS)
    if mojibake_count >= 4:
        return True
    if mojibake_count >= 2 and re.search(r"[÷þðÐÑÒÓ]", value):
        return True
    return False


def repair_mojibake(value: str) -> str:
    value = clean_text(value)
    if not looks_mojibake(value):
        return value
    try:
        repaired = value.encode("latin1").decode("cp1251")
    except UnicodeError:
        return value
    return clean_text(repaired)


def better_unstructured_artist(tag_artist: str, filename_artist: str) -> list[tuple[str, str]]:
    tag_artist = clean_text(tag_artist)
    filename_artist = clean_text(filename_artist)
    if not filename_artist:
        return [(tag_artist, "tag")]
    if not tag_artist:
        return [(filename_artist, "filename")]
    tag_folded = tag_artist.casefold()
    filename_folded = filename_artist.casefold()
    if filename_folded.startswith(tag_folded) and len(filename_artist) > len(tag_artist):
        return [(filename_artist, "filename"), (tag_artist, "tag")]
    if tag_folded.startswith(filename_folded) and len(tag_artist) > len(filename_artist):
        return [(tag_artist, "tag"), (filename_artist, "filename")]
    return [(tag_artist, "tag"), (filename_artist, "filename")]


def first_tag(tags: dict[str, Any], *names: str) -> str:
    for name in names:
        value = clean_text(tags.get(name))
        if value:
            return value
    return ""


def normalize_tag_keys(raw_tags: Any) -> dict[str, Any]:
    if not raw_tags:
        return {}
    normalized: dict[str, Any] = {}
    for key, value in dict(raw_tags).items():
        key_text = str(key).lower()
        if ":" in key_text:
            key_text = key_text.rsplit(":", 1)[-1]
        normalized[key_text] = value
    return normalized


def read_tags(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as exc:
        return {}, {}, {"mutagen_type": "", "parser_error": f"{type(exc).__name__}: {exc}"}
    if audio is None:
        return {}, {}, {"mutagen_type": "", "parser_error": "unsupported_or_unreadable"}
    tags = normalize_tag_keys(audio.tags)
    info = getattr(audio, "info", None)
    info_data = {
        "duration_seconds": round(float(getattr(info, "length", 0.0)), 3) if getattr(info, "length", None) else None,
        "bitrate": getattr(info, "bitrate", None),
        "sample_rate": getattr(info, "sample_rate", None),
        "channels": getattr(info, "channels", None),
    }
    return tags, info_data, {"mutagen_type": type(audio).__name__}


def parse_track_number(value: str) -> int | None:
    value = clean_text(value)
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def clean_album_dir(name: str) -> tuple[str, int | None]:
    value = clean_text(name)
    match = YEAR_ALBUM_RE.match(value)
    if match:
        return clean_text(match.group(2)), int(match.group(1))
    return value, None


def filename_guess(path: Path) -> dict[str, Any]:
    stem = clean_text(path.stem)
    track_number = None
    match = TRACK_PREFIX_RE.match(stem)
    if match:
        track_number = int(match.group(1))
        stem = clean_text(match.group(2))

    artist = ""
    title = stem
    parts = re.split(r"\s+[-–—]\s+", stem, maxsplit=1)
    if len(parts) == 2 and is_sane(parts[0]) and is_sane(parts[1]):
        artist = clean_text(parts[0])
        title = clean_text(parts[1])

    return {"artist": artist, "title": title, "track_number": track_number}


def path_guess(path: Path, music_root: Path) -> dict[str, Any]:
    rel = path.relative_to(music_root)
    parts = rel.parts
    artist = ""
    album = ""
    album_year = None
    source = ""

    if "Artists" in parts:
        idx = parts.index("Artists")
        if idx + 1 < len(parts) - 1:
            artist = clean_text(parts[idx + 1])
            source = "path:Artists"
        remainder = list(parts[idx + 2 : -1])
        remainder = [part for part in remainder if part not in {"Альбомы", "Albums"}]
        if remainder:
            if DISC_DIR_RE.match(remainder[-1]) and len(remainder) >= 2:
                album_name = remainder[-2]
            else:
                album_name = remainder[0]
            album, album_year = clean_album_dir(album_name)
    else:
        parents = list(parts[:-1])
        meaningful = [part for part in parents if part not in GENERIC_DIRS and not DISC_DIR_RE.match(part)]
        if meaningful:
            album, album_year = clean_album_dir(meaningful[-1])
            source = "path:folder"

    return {"artist": artist, "album": album, "year": album_year, "source": source}


def choose(preferred: list[tuple[str, str]], fallback: str) -> Chosen:
    for value, source in preferred:
        value = clean_text(value)
        if is_sane(value):
            return Chosen(value, source)
    return Chosen(fallback, "fallback")


def sort_key(value: str) -> tuple[str, str]:
    folded = value.casefold()
    folded = re.sub(r"^(the|a|an)\s+", "", folded)
    return folded, value


def track_sort_key(track: dict[str, Any]) -> tuple[int, int, str, str]:
    disc = track.get("disc_number") or 0
    number = track.get("track_number") or 9999
    return int(disc), int(number), sort_key(track["title"])[0], track["relative_path"]


def scan_music(root: Path, music_root: Path) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []

    for path in sorted(music_root.rglob("*"), key=lambda p: str(p).casefold()):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue

        stat = path.stat()
        tags, info_data, parser_data = read_tags(path)
        filename = filename_guess(path)
        path_data = path_guess(path, music_root)

        raw_tag_artist = first_tag(tags, "albumartist", "album artist", "artist", "artists", "performer")
        raw_tag_track_artist = first_tag(tags, "artist", "artists", "performer")
        raw_tag_album = first_tag(tags, "album")
        raw_tag_title = first_tag(tags, "title")
        tag_artist = repair_mojibake(raw_tag_artist)
        tag_track_artist = repair_mojibake(raw_tag_track_artist)
        tag_album = repair_mojibake(raw_tag_album)
        tag_title = repair_mojibake(raw_tag_title)
        tag_track = first_tag(tags, "tracknumber", "track")
        tag_disc = first_tag(tags, "discnumber", "disc")
        tag_date = first_tag(tags, "date", "year", "originaldate")
        tag_genre = first_tag(tags, "genre")

        if path_data["artist"]:
            artist_candidates = [(path_data["artist"], "path"), (tag_artist, "tag"), (filename["artist"], "filename")]
        else:
            artist_candidates = better_unstructured_artist(tag_artist, filename["artist"])
        artist = choose(artist_candidates, "Unknown Artist")
        album = choose(
            [
                (path_data["album"], "path"),
                (tag_album, "tag"),
            ],
            "Unknown Album",
        )
        title = choose(
            [
                (tag_title, "tag"),
                (filename["title"], "filename"),
            ],
            path.name,
        )

        year = path_data["year"] or parse_track_number(tag_date)
        track_number = parse_track_number(tag_track) or filename["track_number"]
        disc_number = parse_track_number(tag_disc)
        warnings: list[str] = []
        if parser_data.get("parser_error"):
            warnings.append("tag_read_error")
        for field_name, value in {
            "tag_artist": raw_tag_artist,
            "tag_album": raw_tag_album,
            "tag_title": raw_tag_title,
        }.items():
            if value and not is_sane(value):
                warnings.append(f"suspicious_{field_name}")
        if artist.value == "Unknown Artist":
            warnings.append("unknown_artist")
        if album.value == "Unknown Album":
            warnings.append("unknown_album")
        if title.source == "fallback":
            warnings.append("unknown_title")

        tracks.append(
            {
                "relative_path": str(path.relative_to(root)),
                "filename": path.name,
                "extension": path.suffix.lower().lstrip("."),
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "artist": artist.value,
                "artist_source": artist.source,
                "album_artist_tag": raw_tag_artist,
                "track_artist_tag": raw_tag_track_artist,
                "album_artist_repaired": tag_artist if tag_artist != raw_tag_artist else "",
                "track_artist_repaired": tag_track_artist if tag_track_artist != raw_tag_track_artist else "",
                "album": album.value,
                "album_source": album.source,
                "title": title.value,
                "title_source": title.source,
                "track_number": track_number,
                "disc_number": disc_number,
                "year": year,
                "genre": tag_genre,
                "tag_album": raw_tag_album,
                "tag_album_repaired": tag_album if tag_album != raw_tag_album else "",
                "tag_title": raw_tag_title,
                "tag_title_repaired": tag_title if tag_title != raw_tag_title else "",
                "tag_track": tag_track,
                "tag_disc": tag_disc,
                "tag_date": tag_date,
                "path_album_source": path_data["source"],
                "warnings": warnings,
                "metadata_sources": {
                    "path": {
                        "artist": path_data["artist"],
                        "album": path_data["album"],
                        "year": path_data["year"],
                        "source": path_data["source"],
                    },
                    "filename": {
                        "artist": filename["artist"],
                        "title": filename["title"],
                        "track_number": filename["track_number"],
                    },
                    "tags": {
                        "album_artist": raw_tag_artist,
                        "track_artist": raw_tag_track_artist,
                        "album_artist_repaired": tag_artist if tag_artist != raw_tag_artist else "",
                        "track_artist_repaired": tag_track_artist if tag_track_artist != raw_tag_track_artist else "",
                        "album": raw_tag_album,
                        "album_repaired": tag_album if tag_album != raw_tag_album else "",
                        "title": raw_tag_title,
                        "title_repaired": tag_title if tag_title != raw_tag_title else "",
                        "track": tag_track,
                        "disc": tag_disc,
                        "date": tag_date,
                        "genre": tag_genre,
                    },
                    "selected": {
                        "artist": {"value": artist.value, "source": artist.source},
                        "album": {"value": album.value, "source": album.source},
                        "title": {"value": title.value, "source": title.source},
                    },
                },
                **info_data,
                **parser_data,
            }
        )

    return tracks


def load_fingerprint_cache(path: Path) -> dict[tuple[str, int, str], dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    cache: dict[tuple[str, int, str], dict[str, Any]] = {}
    for track in data.get("tracks", []):
        fingerprint = track.get("fingerprint")
        if not isinstance(fingerprint, dict) or not (fingerprint.get("value") or fingerprint.get("error")):
            continue
        key = (track.get("relative_path", ""), int(track.get("size_bytes") or 0), track.get("mtime", ""))
        cache[key] = fingerprint
    return cache


def load_hash_cache(path: Path) -> dict[tuple[str, int, str], dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    cache: dict[tuple[str, int, str], dict[str, Any]] = {}
    for track in data.get("tracks", []):
        integrity = track.get("file_integrity")
        if not isinstance(integrity, dict) or not integrity.get("sha256"):
            continue
        key = (track.get("relative_path", ""), int(track.get("size_bytes") or 0), track.get("mtime", ""))
        cache[key] = integrity
    return cache


def sha256_file(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "sha256": digest.hexdigest(),
        "algorithm": "sha256",
    }


def add_file_hashes(tracks: list[dict[str, Any]], root: Path, cache_path: Path) -> None:
    cache = load_hash_cache(cache_path)
    pending: list[dict[str, Any]] = []

    for track in tracks:
        key = (track["relative_path"], int(track.get("size_bytes") or 0), track.get("mtime", ""))
        cached = cache.get(key)
        if cached:
            track["file_integrity"] = cached
            continue
        pending.append(track)

    if pending:
        print(f"hashes_to_compute={len(pending)} workers={HASH_WORKERS}", flush=True)

    done = 0
    with ThreadPoolExecutor(max_workers=HASH_WORKERS) as executor:
        futures = {
            executor.submit(sha256_file, root / track["relative_path"]): track
            for track in pending
        }
        for future in as_completed(futures):
            track = futures[future]
            done += 1
            try:
                track["file_integrity"] = future.result()
            except Exception as exc:
                track["file_integrity"] = {
                    "algorithm": "sha256",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                if "hash_error" not in track["warnings"]:
                    track["warnings"].append("hash_error")
            if done % 100 == 0 or done == len(pending):
                print(f"hashes_done={done}/{len(pending)}", flush=True)


def run_fpcalc(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["fpcalc", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    duration = None
    value = ""
    for line in proc.stdout.splitlines():
        if line.startswith("DURATION="):
            duration = int(line.split("=", 1)[1])
        elif line.startswith("FINGERPRINT="):
            value = line.split("=", 1)[1].strip()
    diagnostics = "\n".join(
        line
        for line in (proc.stdout + "\n" + proc.stderr).splitlines()
        if line.startswith("ERROR:")
    )
    if not value:
        detail = diagnostics or f"fpcalc exited with code {proc.returncode}"
        raise ValueError(detail)
    result = {
        "algorithm": FINGERPRINT_ALGORITHM,
        "duration_seconds": duration,
        "value": value,
    }
    if proc.returncode != 0:
        result["warning"] = diagnostics or f"fpcalc exited with code {proc.returncode}"
    return result


def add_fingerprints(tracks: list[dict[str, Any]], root: Path, cache_path: Path) -> None:
    if shutil.which("fpcalc") is None:
        print("warning: fpcalc not found; skipping Chromaprint fingerprints", file=sys.stderr)
        return

    cache = load_fingerprint_cache(cache_path)
    pending: list[dict[str, Any]] = []

    for track in tracks:
        key = (track["relative_path"], int(track.get("size_bytes") or 0), track.get("mtime", ""))
        cached = cache.get(key)
        if cached:
            track["fingerprint"] = cached
            continue
        pending.append(track)

    if pending:
        print(f"fingerprints_to_compute={len(pending)} workers={FINGERPRINT_WORKERS}", flush=True)

    done = 0
    with ThreadPoolExecutor(max_workers=FINGERPRINT_WORKERS) as executor:
        futures = {
            executor.submit(run_fpcalc, root / track["relative_path"]): track
            for track in pending
        }
        for future in as_completed(futures):
            track = futures[future]
            done += 1
            try:
                track["fingerprint"] = future.result()
            except Exception as exc:
                track["fingerprint"] = {
                    "algorithm": FINGERPRINT_ALGORITHM,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                if "fingerprint_error" not in track["warnings"]:
                    track["warnings"].append("fingerprint_error")
            if done % 100 == 0 or done == len(pending):
                print(f"fingerprints_done={done}/{len(pending)}", flush=True)


def nested_index(tracks: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    index: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for track in tracks:
        index[track["artist"]][track["album"]].append(track)
    return index


def md_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def render_markdown(tracks: list[dict[str, Any]], app_dir_name: str = ".") -> str:
    index = nested_index(tracks)
    artists = sorted(index, key=sort_key)
    total_albums = sum(len(albums) for albums in index.values())
    warning_tracks = [track for track in tracks if track["warnings"]]
    unknown_artist = sum(1 for track in tracks if track["artist"] == "Unknown Artist")
    unknown_album = sum(1 for track in tracks if track["album"] == "Unknown Album")
    fingerprinted = sum(1 for track in tracks if track.get("fingerprint", {}).get("value"))
    fingerprint_errors = sum(1 for track in tracks if track.get("fingerprint", {}).get("error"))
    hashed = sum(1 for track in tracks if track.get("file_integrity", {}).get("sha256"))
    hash_errors = sum(1 for track in tracks if track.get("file_integrity", {}).get("error"))

    lines = [
        "# Music",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Summary",
        "",
        f"- Audio files in `Music`: {len(tracks)}",
        f"- Artists: {len(artists)}",
        f"- Albums/groups: {total_albums}",
        f"- Tracks with metadata warnings: {len(warning_tracks)}",
        f"- Tracks with unknown artist: {unknown_artist}",
        f"- Tracks with unknown album: {unknown_album}",
        f"- Tracks with Chromaprint fingerprint: {fingerprinted}",
        f"- Tracks with fingerprint errors: {fingerprint_errors}",
        f"- Tracks with SHA256 hash: {hashed}",
        f"- Tracks with hash errors: {hash_errors}",
        "",
        f"Raw data and interactive view: `{app_dir_name}/music.json`, `{app_dir_name}/index.html`.",
        "",
    ]

    lines += ["## Исполнители", ""]
    for artist in artists:
        album_count = len(index[artist])
        track_count = sum(len(album_tracks) for album_tracks in index[artist].values())
        lines.append(f"- {md_escape(artist)} ({album_count} albums/groups, {track_count} tracks)")
    lines.append("")

    lines += ["## Альбомы", ""]
    for artist in artists:
        lines.append(f"- {md_escape(artist)}")
        albums = sorted(index[artist], key=sort_key)
        for album in albums:
            album_tracks = index[artist][album]
            years = sorted({track["year"] for track in album_tracks if track["year"]})
            year_text = f", {years[0]}" if len(years) == 1 else ""
            lines.append(f"  - {md_escape(album)} ({len(album_tracks)} tracks{year_text})")
    lines.append("")

    lines += ["## Треки", ""]
    for artist in artists:
        lines.append(f"- {md_escape(artist)}")
        albums = sorted(index[artist], key=sort_key)
        for album in albums:
            album_tracks = sorted(index[artist][album], key=track_sort_key)
            lines.append(f"  - {md_escape(album)}")
            for track in album_tracks:
                prefix_parts = []
                if track.get("disc_number"):
                    prefix_parts.append(f"D{track['disc_number']}")
                if track.get("track_number"):
                    prefix_parts.append(f"{int(track['track_number']):02d}")
                prefix = f"{'.'.join(prefix_parts)}. " if prefix_parts else ""
                warning = " [metadata warning]" if track["warnings"] else ""
                lines.append(f"    - {prefix}{md_escape(track['title'])} `{track['relative_path']}`{warning}")
    lines.append("")

    lines += ["## Metadata Warnings", ""]
    if warning_tracks:
        for track in sorted(warning_tracks, key=lambda t: t["relative_path"].casefold()):
            lines.append(
                f"- `{track['relative_path']}`: {', '.join(track['warnings'])}; "
                f"chosen `{track['artist']} / {track['album']} / {track['title']}`"
            )
    else:
        lines.append("- None")
    lines.append("")

    return "\n".join(lines)


def render_html() -> str:
    return """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Music Archive</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header>
    <h1>Music Archive</h1>
    <div class="controls">
      <input id="q" placeholder="Search artist, album, title, path">
      <div class="combo-filter" data-filter="artist">
        <button class="combo-button" id="artistButton" type="button" aria-expanded="false">All artists</button>
        <div class="combo-panel" id="artistPanel" hidden>
          <input class="combo-search" id="artistSearch" placeholder="Search artists">
          <div class="combo-options" id="artistOptions"></div>
        </div>
      </div>
      <div class="combo-filter" data-filter="album">
        <button class="combo-button" id="albumButton" type="button" aria-expanded="false">All albums</button>
        <div class="combo-panel" id="albumPanel" hidden>
          <input class="combo-search" id="albumSearch" placeholder="Search albums">
          <div class="combo-options" id="albumOptions"></div>
        </div>
      </div>
      <div class="issue-filter" id="issueFilter">
        <button class="issue-filter-button" id="issueButton" type="button" aria-expanded="false">Issues</button>
        <div class="issue-chips" id="issueChips"></div>
        <div class="issue-panel" id="issuePanel" hidden></div>
      </div>
    </div>
  </header>
  <main>
    <div class="stats" id="stats"></div>
    <table>
      <thead>
        <tr>
          <th class="play-col"></th>
          <th class="num" data-sort="_index">#</th>
          <th class="num" data-sort="track_number">Trk</th>
          <th data-sort="artist">Artist</th>
          <th data-sort="album">Album</th>
          <th data-sort="title">Track</th>
          <th data-sort="issues">Issues</th>
          <th class="optional" data-sort="year">Year</th>
          <th class="optional" data-sort="relative_path">Path</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </main>
  <div class="player" id="player" hidden>
    <div class="player-progress" id="playerProgress"></div>
    <button class="player-toggle" id="playerToggle" type="button" aria-label="Play preview"></button>
    <div class="player-meta" id="playerSeek">
      <strong id="playerTitle">Preview</strong>
      <span id="playerStatus"></span>
    </div>
    <span class="player-time" id="playerTime">0:00</span>
    <audio id="previewAudio"></audio>
  </div>
  <script src="app.js"></script>
</body>
</html>
"""


def render_styles() -> str:
    return """:root {
  color-scheme: light dark;
  --line: #8a8f981f;
  --muted: #667085;
  --header-height: 88px;
}

body {
  margin: 0;
  font: 14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
}

header {
  position: sticky;
  top: 0;
  z-index: 2;
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
  background: Canvas;
}

h1 {
  margin: 0 0 10px;
  font-size: 20px;
}

.controls {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(130px, 220px) minmax(130px, 220px) minmax(220px, 340px);
  gap: 8px;
  align-items: start;
}

input,
button {
  width: 100%;
  box-sizing: border-box;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: Canvas;
  color: CanvasText;
  font: inherit;
}

button {
  cursor: pointer;
}

.combo-filter {
  position: relative;
  min-width: 0;
}

.combo-button {
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.combo-button.active {
  border-color: color-mix(in srgb, Highlight 60%, var(--line));
}

.combo-panel {
  position: absolute;
  left: 0;
  top: calc(100% + 6px);
  z-index: 5;
  width: min(360px, calc(100vw - 36px));
  max-height: 360px;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 16px 36px #00000024;
  background: Canvas;
}

.combo-search {
  margin-bottom: 8px;
}

.combo-options {
  max-height: 292px;
  overflow: auto;
}

.combo-option {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px;
  width: 100%;
  padding: 7px 6px;
  border: 0;
  border-radius: 6px;
  text-align: left;
  background: transparent;
}

.combo-option:hover,
.combo-option.selected {
  background: color-mix(in srgb, CanvasText 6%, Canvas);
}

.combo-option-count {
  color: var(--muted);
  font-size: 11px;
}

main {
  padding: 16px 18px 70px;
}

.stats {
  color: var(--muted);
  margin-bottom: 12px;
}

table {
  border-collapse: collapse;
  width: 100%;
  table-layout: fixed;
}

th,
td {
  border-bottom: 1px solid var(--line);
  padding: 7px 8px;
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}

th {
  position: sticky;
  top: var(--header-height);
  background: Canvas;
  z-index: 1;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
}

tbody tr.track-row {
  cursor: pointer;
}

tbody tr.track-row:hover {
  background: color-mix(in srgb, CanvasText 4%, Canvas);
}

th[data-sort]::after {
  content: "↕";
  margin-left: 4px;
  color: color-mix(in srgb, var(--muted) 65%, Canvas);
  font-size: 11px;
}

th.sorted-asc::after {
  content: "↑";
  color: CanvasText;
}

th.sorted-desc::after {
  content: "↓";
  color: CanvasText;
}

.num {
  width: 54px;
  text-align: right;
}

.play-col {
  width: 42px;
  text-align: center;
}

.preview-button {
  --preview-progress: 0%;
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 50%;
  background:
    linear-gradient(Canvas, Canvas) padding-box,
    conic-gradient(CanvasText var(--preview-progress), transparent 0) border-box;
}

.preview-button:hover {
  border-color: var(--line);
  background: color-mix(in srgb, Highlight 10%, Canvas);
}

.preview-button.active {
  border-color: color-mix(in srgb, Highlight 50%, var(--line));
  background:
    linear-gradient(color-mix(in srgb, Highlight 14%, Canvas), color-mix(in srgb, Highlight 14%, Canvas)) padding-box,
    conic-gradient(CanvasText var(--preview-progress), color-mix(in srgb, CanvasText 12%, transparent) 0) border-box;
}

.preview-button.error {
  border-color: color-mix(in srgb, #d9480f 45%, var(--line));
  color: #b42318;
  background:
    linear-gradient(color-mix(in srgb, #f97316 16%, Canvas), color-mix(in srgb, #f97316 16%, Canvas)) padding-box,
    conic-gradient(#b42318 100%, transparent 0) border-box;
}

.preview-button svg {
  width: 15px;
  height: 15px;
  fill: currentColor;
}

.details-row td {
  padding: 0;
  background: color-mix(in srgb, CanvasText 3%, Canvas);
}

.details {
  display: grid;
  grid-template-columns: repeat(4, minmax(160px, 1fr));
  gap: 10px;
  padding: 12px 14px;
}

.details-section {
  min-width: 0;
}

.details-section h3 {
  margin: 0 0 6px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
}

.details-section dl {
  margin: 0;
}

.details-section dt {
  color: var(--muted);
  font-size: 11px;
}

.details-section dd {
  margin: 0 0 5px;
  overflow-wrap: anywhere;
  font-size: 12px;
}

.copy-value {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
}

.copy-value button {
  width: auto;
  padding: 1px 6px;
  border-radius: 999px;
  color: var(--muted);
  font-size: 11px;
}

.path {
  color: var(--muted);
  font-size: 12px;
}

.warn {
  color: #b54708;
  font-size: 12px;
}

.issue-filter {
  position: relative;
  min-width: 0;
}

.issue-filter-button {
  text-align: left;
}

.issue-filter-button.active {
  border-color: color-mix(in srgb, Highlight 60%, var(--line));
}

.issue-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}

.filter-chip {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  gap: 4px;
  padding: 2px 6px;
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: 11px;
  background: color-mix(in srgb, Highlight 10%, Canvas);
}

.filter-chip button {
  width: auto;
  padding: 0 2px;
  border: 0;
  border-radius: 50%;
  color: var(--muted);
  background: transparent;
}

.filter-chip button:hover {
  color: CanvasText;
}

.issue-panel {
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 5;
  width: min(360px, calc(100vw - 36px));
  max-height: 340px;
  overflow: auto;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 16px 36px #00000024;
  background: Canvas;
}

.issue-option {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 8px;
  align-items: start;
  padding: 7px 6px;
  border-radius: 6px;
}

.issue-option:hover {
  background: color-mix(in srgb, CanvasText 6%, Canvas);
}

.issue-option input {
  width: 16px;
  height: 16px;
  padding: 0;
  margin-top: 3px;
}

.issue-option-code {
  font-size: 12px;
}

.issue-option-help {
  display: block;
  color: var(--muted);
  font-size: 11px;
}

.issue-option-count {
  color: var(--muted);
  font-size: 11px;
}

.issues {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.issue {
  display: inline-flex;
  max-width: 100%;
  padding: 2px 6px;
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: 11px;
  color: CanvasText;
  background: color-mix(in srgb, CanvasText 6%, Canvas);
}

.issue.error {
  color: #b42318;
}

.issue.warning {
  color: #b54708;
}

.actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 4px;
}

.actions a {
  color: LinkText;
  font-size: 12px;
  text-decoration: none;
}

.actions button,
.actions a {
  width: auto;
  padding: 0;
  border: 0;
  color: LinkText;
  background: transparent;
  font-size: 12px;
  text-decoration: none;
}

.actions a:hover,
.actions button:hover {
  text-decoration: underline;
}

.player {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 6;
  display: grid;
  grid-template-columns: 40px minmax(160px, 1fr) 82px;
  gap: 10px;
  align-items: center;
  min-height: 46px;
  padding: 6px 14px;
  border-top: 1px solid var(--line);
  box-shadow: 0 -12px 30px #0000001a;
  background: Canvas;
}

.player[hidden] {
  display: none;
}

.player-progress {
  position: absolute;
  inset: 0 auto 0 0;
  width: 0%;
  pointer-events: none;
  background: color-mix(in srgb, Highlight 18%, transparent);
}

.player.error .player-progress {
  width: 100%;
  background: color-mix(in srgb, #f97316 18%, transparent);
}

.player.error .player-toggle {
  border-color: color-mix(in srgb, #d9480f 45%, var(--line));
  color: #b42318;
  background: color-mix(in srgb, #f97316 12%, Canvas);
}

.player-toggle {
  position: relative;
  z-index: 1;
  display: grid;
  place-items: center;
  min-width: 0;
  width: 32px;
  height: 32px;
  padding: 0;
  border-radius: 999px;
}

.player-toggle svg {
  width: 16px;
  height: 16px;
  fill: currentColor;
}

.player-meta {
  position: relative;
  z-index: 1;
  min-width: 0;
  cursor: pointer;
}

.player-meta strong,
.player-meta span {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.player-meta span {
  color: var(--muted);
  font-size: 12px;
}

.player-time {
  position: relative;
  z-index: 1;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  text-align: right;
  white-space: nowrap;
}

.player audio {
  display: none;
}

@media (max-width: 820px) {
  .controls {
    grid-template-columns: 1fr;
  }
  .optional {
    display: none;
  }

  .details {
    grid-template-columns: 1fr;
  }

  .player {
    grid-template-columns: 40px minmax(120px, 1fr) 82px;
  }
}
"""


def render_app_js() -> str:
    return """let tracks = [];
    let duplicateGroups = new Map();
    let artistOptionsData = [];
    let albumOptionsAll = [];
    let albumOptionsByArtist = new Map();
    let issueOptionsData = [];
    let sortField = "artist";
    let sortDirection = 1;
    const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
    const issueDescriptions = {
      duplicate_fingerprint: "Same Chromaprint fingerprint appears on multiple files.",
      fingerprint_error: "Chromaprint fingerprint could not be calculated.",
      fingerprint_warning: "fpcalc produced a fingerprint but reported a read warning.",
      hash_error: "SHA256 hash could not be calculated.",
      suspicious_tag_artist: "Artist tag looks like a URL, mojibake, spam, or otherwise unreliable metadata.",
      suspicious_tag_album: "Album tag looks like a URL, mojibake, spam, or otherwise unreliable metadata.",
      suspicious_tag_title: "Title tag looks like a URL, mojibake, spam, or otherwise unreliable metadata.",
      tag_read_error: "Metadata reader could not fully parse tags for this file.",
      unknown_album: "Album could not be inferred from path or reliable tags.",
      unknown_artist: "Artist could not be inferred from path, reliable tags, or filename.",
      unknown_title: "Title could not be inferred from reliable tags or filename."
    };

    const q = document.getElementById("q");
    const artistButton = document.getElementById("artistButton");
    const artistPanel = document.getElementById("artistPanel");
    const artistSearch = document.getElementById("artistSearch");
    const artistOptions = document.getElementById("artistOptions");
    const albumButton = document.getElementById("albumButton");
    const albumPanel = document.getElementById("albumPanel");
    const albumSearch = document.getElementById("albumSearch");
    const albumOptions = document.getElementById("albumOptions");
    const issueFilter = document.getElementById("issueFilter");
    const issueButton = document.getElementById("issueButton");
    const issueChips = document.getElementById("issueChips");
    const issuePanel = document.getElementById("issuePanel");
    const header = document.querySelector("header");
    const rows = document.getElementById("rows");
    const stats = document.getElementById("stats");
    const player = document.getElementById("player");
    const playerTitle = document.getElementById("playerTitle");
    const playerStatus = document.getElementById("playerStatus");
    const playerProgress = document.getElementById("playerProgress");
    const playerToggle = document.getElementById("playerToggle");
    const playerSeek = document.getElementById("playerSeek");
    const playerTime = document.getElementById("playerTime");
    const previewAudio = document.getElementById("previewAudio");
    const filters = {artist: "", album: ""};
    const activeIssues = new Set();
    const expandedRows = new Set();
    const trackByIndex = new Map();
    let currentPreviewIndex = null;
    let previewErrorIndex = null;
    const playIcon = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"></path></svg>`;
    const pauseIcon = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h4v14H7zM13 5h4v14h-4z"></path></svg>`;
    const errorIcon = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5h2v10h-2zM11 17h2v2h-2z"></path></svg>`;

    async function loadData() {
      try {
        const response = await fetch("music.json");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        tracks = data.tracks ?? [];
        tracks.forEach((track, index) => { track._index = index + 1; });
        trackByIndex.clear();
        tracks.forEach(track => trackByIndex.set(track._index, track));
        prepareDerivedData();
        render();
      } catch (error) {
        stats.textContent = `Could not load music.json: ${error.message}`;
      }
    }

    function prepareDerivedData() {
      duplicateGroups = new Map();
      for (const track of tracks) {
        const fingerprint = track.fingerprint?.value;
        if (!fingerprint) continue;
        if (!duplicateGroups.has(fingerprint)) duplicateGroups.set(fingerprint, []);
        duplicateGroups.get(fingerprint).push(track);
      }
      duplicateGroups = new Map([...duplicateGroups].filter(([, group]) => group.length > 1));
      for (const track of tracks) {
        track._issues = collectIssues(track);
        track._search = [track.artist, track.album, track.title, track.relative_path].join(" ").toLowerCase();
      }
      prepareFilterIndexes();
    }

    function optionList(values) {
      return [...new Set(values.filter(Boolean))].sort(collator.compare);
    }

    function prepareFilterIndexes() {
      artistOptionsData = optionData(tracks, track => track.artist);
      albumOptionsAll = albumOptionData(tracks);
      albumOptionsByArtist = new Map();
      const tracksByArtist = new Map();
      for (const track of tracks) {
        if (!track.artist) continue;
        if (!tracksByArtist.has(track.artist)) tracksByArtist.set(track.artist, []);
        tracksByArtist.get(track.artist).push(track);
      }
      for (const [artist, artistTracks] of tracksByArtist) {
        albumOptionsByArtist.set(artist, albumOptionData(artistTracks));
      }
      issueOptionsData = [...issueCounts()].map(([value, count]) => ({value, count})).sort((a, b) => collator.compare(a.value, b.value));
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }

    function countValues(items, getter) {
      const counts = new Map();
      for (const item of items) {
        const value = getter(item);
        if (!value) continue;
        counts.set(value, (counts.get(value) ?? 0) + 1);
      }
      return counts;
    }

    function optionData(items, getter) {
      return [...countValues(items, getter)]
        .map(([value, count]) => ({value, count}))
        .sort((a, b) => collator.compare(a.value, b.value));
    }

    function albumOptionData(items) {
      const groups = new Map();
      for (const track of items) {
        if (!track.album) continue;
        if (!groups.has(track.album)) groups.set(track.album, {value: track.album, count: 0, years: new Set()});
        const group = groups.get(track.album);
        group.count += 1;
        if (track.year) group.years.add(track.year);
      }
      return [...groups.values()]
        .map(group => ({
          value: group.value,
          count: group.count,
          year: group.years.size === 1 ? [...group.years][0] : ""
        }))
        .sort((a, b) => collator.compare(a.value, b.value));
    }

    function renderCombo(kind) {
      const isArtist = kind === "artist";
      const button = isArtist ? artistButton : albumButton;
      const panel = isArtist ? artistPanel : albumPanel;
      const search = isArtist ? artistSearch : albumSearch;
      const options = isArtist ? artistOptions : albumOptions;
      const label = isArtist ? "All artists" : "All albums";
      const data = isArtist
        ? artistOptionsData
        : (filters.artist ? albumOptionsByArtist.get(filters.artist) ?? [] : albumOptionsAll);
      const needle = search.value.trim().toLowerCase();
      const values = data.filter(option => !needle || option.value.toLowerCase().includes(needle));
      button.textContent = filters[kind] || label;
      button.title = filters[kind] || label;
      button.classList.toggle("active", Boolean(filters[kind]));
      button.setAttribute("aria-expanded", String(!panel.hidden));
      if (panel.hidden) {
        updateHeaderHeight();
        return;
      }
      options.innerHTML = [
        `<button class="combo-option ${filters[kind] ? "" : "selected"}" type="button" data-value=""><span>${escapeHtml(label)}</span><span class="combo-option-count">${data.reduce((sum, option) => sum + option.count, 0)}</span></button>`,
        ...values.map(option => {
          const year = isArtist ? "" : option.year;
          const label = year ? `${option.value} (${year})` : option.value;
          return `
          <button class="combo-option ${filters[kind] === option.value ? "selected" : ""}" type="button" data-value="${escapeHtml(option.value)}">
            <span>${escapeHtml(label)}</span>
            <span class="combo-option-count">${option.count}</span>
          </button>
        `;
        })
      ].join("");
      options.querySelectorAll("[data-value]").forEach(option => {
        option.addEventListener("click", () => {
          filters[kind] = option.dataset.value;
          if (kind === "artist" && filters.album) {
            const albumStillAvailable = tracks.some(track => track.artist === filters.artist && track.album === filters.album);
            if (!albumStillAvailable) filters.album = "";
          }
          panel.hidden = true;
          search.value = "";
          render();
        });
      });
      updateHeaderHeight();
    }

    function toggleCombo(kind) {
      const panel = kind === "artist" ? artistPanel : albumPanel;
      const search = kind === "artist" ? artistSearch : albumSearch;
      artistPanel.hidden = kind !== "artist" || !artistPanel.hidden;
      albumPanel.hidden = kind !== "album" || !albumPanel.hidden;
      issuePanel.hidden = true;
      renderCombos();
      if (!panel.hidden) {
        search.focus();
        search.select();
      }
    }

    function renderCombos() {
      renderCombo("artist");
      renderCombo("album");
    }

    function collectIssues(track) {
      const issues = new Set(track.warnings ?? []);
      if (track.fingerprint?.error) issues.add("fingerprint_error");
      if (track.fingerprint?.warning) issues.add("fingerprint_warning");
      if (track.file_integrity?.error) issues.add("hash_error");
      const fingerprint = track.fingerprint?.value;
      if (fingerprint && duplicateGroups.has(fingerprint)) issues.add("duplicate_fingerprint");
      return [...issues].sort(collator.compare);
    }

    function renderIssues(track) {
      const issues = track._issues ?? [];
      if (!issues.length) return "";
      return `<div class="issues">${issues.map(code => {
        const severity = code.includes("error") ? "error" : "warning";
        let title = issueDescriptions[code] ?? code;
        if (code === "duplicate_fingerprint") {
          const group = duplicateGroups.get(track.fingerprint.value) ?? [];
          const files = group.map(item => `#${item._index} ${item.relative_path}`).join("\\n");
          title = `${title}:\\n${files}`;
        }
        return `<span class="issue ${severity}" title="${escapeHtml(title)}">${escapeHtml(code)}</span>`;
      }).join("")}</div>`;
    }

    function selectedIssues() {
      return [...activeIssues];
    }

    function updateHeaderHeight() {
      document.documentElement.style.setProperty("--header-height", `${Math.ceil(header.getBoundingClientRect().height)}px`);
    }

    function issueCounts() {
      const counts = new Map();
      for (const track of tracks) {
        for (const code of track._issues ?? []) counts.set(code, (counts.get(code) ?? 0) + 1);
      }
      return counts;
    }

    function renderIssueFilter() {
      issueButton.textContent = activeIssues.size ? `Issues (${activeIssues.size})` : "Issues";
      issueButton.classList.toggle("active", activeIssues.size > 0);
      issueButton.setAttribute("aria-expanded", String(!issuePanel.hidden));
      if (!issuePanel.hidden) {
        issuePanel.innerHTML = issueOptionsData.map(({value, count}) => `
          <label class="issue-option" title="${escapeHtml(issueDescriptions[value] ?? value)}">
            <input type="checkbox" value="${escapeHtml(value)}" ${activeIssues.has(value) ? "checked" : ""}>
            <span>
              <span class="issue-option-code">${escapeHtml(value)}</span>
              <span class="issue-option-help">${escapeHtml(issueDescriptions[value] ?? value)}</span>
            </span>
            <span class="issue-option-count">${count}</span>
          </label>
        `).join("");
      }
      issueChips.innerHTML = selectedIssues().sort(collator.compare).map(code => `
        <span class="filter-chip" title="${escapeHtml(issueDescriptions[code] ?? code)}">
          ${escapeHtml(code)}
          <button type="button" data-remove-issue="${escapeHtml(code)}" aria-label="Remove ${escapeHtml(code)}">x</button>
        </span>
      `).join("");
      if (!issuePanel.hidden) {
        issuePanel.querySelectorAll("input[type='checkbox']").forEach(input => {
          input.addEventListener("change", () => {
            if (input.checked) activeIssues.add(input.value);
            else activeIssues.delete(input.value);
            render();
          });
        });
      }
      issueChips.querySelectorAll("[data-remove-issue]").forEach(button => {
        button.addEventListener("click", () => {
          activeIssues.delete(button.dataset.removeIssue);
          render();
        });
      });
      updateHeaderHeight();
    }

    function formatDuration(seconds) {
      seconds = Math.round(seconds || 0);
      const days = Math.floor(seconds / 86400);
      seconds %= 86400;
      const hours = Math.floor(seconds / 3600);
      seconds %= 3600;
      const minutes = Math.floor(seconds / 60);
      const parts = [];
      if (days) parts.push(`${days}d`);
      if (hours) parts.push(`${hours}h`);
      parts.push(`${minutes}m`);
      return parts.join(" ");
    }

    function renderStats(data) {
      const artists = new Set(data.map(track => track.artist).filter(Boolean));
      const albums = new Set(data.map(track => `${track.artist}\\u0000${track.album}`).filter(Boolean));
      const duration = data.reduce((sum, track) => sum + (Number(track.duration_seconds) || 0), 0);
      const issueTracks = data.filter(track => track._issues?.length).length;
      const visibleDuplicateGroups = new Set(
        data
          .filter(track => track.fingerprint?.value && duplicateGroups.has(track.fingerprint.value))
          .map(track => track.fingerprint.value)
      );
      stats.textContent = `${data.length} of ${tracks.length} tracks · ${artists.size} artists · ${albums.size} albums/groups · ${formatDuration(duration)} · ${issueTracks} with issues · ${visibleDuplicateGroups.size} duplicate groups`;
    }

    function sortValue(track, field) {
      if (field === "issues") return (track._issues ?? []).join(" ");
      return track[field] ?? "";
    }

    function renderSortIndicators() {
      document.querySelectorAll("th[data-sort]").forEach(th => {
        const active = th.dataset.sort === sortField;
        th.classList.toggle("sorted-asc", active && sortDirection === 1);
        th.classList.toggle("sorted-desc", active && sortDirection === -1);
      });
    }

    function searchUrl(track, service) {
      const query = encodeURIComponent([track.artist, track.title].filter(Boolean).join(" "));
      if (service === "spotify") return `https://open.spotify.com/search/${query}`;
      if (service === "youtube") return `https://www.youtube.com/results?search_query=${query}`;
      return `https://musicbrainz.org/search?query=${query}&type=recording&method=indexed`;
    }

    function formatBytes(bytes) {
      bytes = Number(bytes) || 0;
      if (!bytes) return "";
      const units = ["B", "KB", "MB", "GB"];
      let value = bytes;
      let unit = 0;
      while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
      }
      return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
    }

    function metadataValue(value) {
      if (value === null || value === undefined || value === "") return "";
      if (Array.isArray(value)) return value.join(", ");
      if (typeof value === "object") return JSON.stringify(value);
      return String(value);
    }

    function detailList(entries) {
      return entries
        .filter(([, value]) => metadataValue(value))
        .map(([key, value]) => {
          if (key === "fingerprint") {
            return `<dt>${escapeHtml(key)}</dt><dd>${renderCopyValue(value)}</dd>`;
          }
          return `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(metadataValue(value))}</dd>`;
        })
        .join("");
    }

    function compactValue(value, edge = 6) {
      value = metadataValue(value);
      if (value.length <= edge * 2 + 3) return value;
      return `${value.slice(0, edge)}...${value.slice(-edge)}`;
    }

    function renderCopyValue(value) {
      value = metadataValue(value);
      if (!value) return "";
      return `<span class="copy-value">${escapeHtml(compactValue(value))}<button type="button" data-copy-value="${escapeHtml(value)}">Copy</button></span>`;
    }

    function detailSection(title, entries) {
      const body = detailList(entries);
      if (!body) return "";
      return `<section class="details-section"><h3>${escapeHtml(title)}</h3><dl>${body}</dl></section>`;
    }

    function renderDetails(track) {
      const sources = track.metadata_sources ?? {};
      return `
        <div class="details">
          ${detailSection("Selected", [
            ["artist", track.artist],
            ["album", track.album],
            ["title", track.title],
            ["artist source", track.artist_source],
            ["album source", track.album_source],
            ["title source", track.title_source],
            ["year", track.year],
            ["genre", track.genre],
            ["track", track.track_number],
            ["disc", track.disc_number]
          ])}
          ${detailSection("File", [
            ["path", track.relative_path],
            ["filename", track.filename],
            ["extension", track.extension],
            ["size", formatBytes(track.size_bytes)],
            ["mtime", track.mtime],
            ["duration", track.duration_seconds ? `${Math.round(track.duration_seconds)}s` : ""],
            ["bitrate", track.bitrate],
            ["sample rate", track.sample_rate],
            ["channels", track.channels],
            ["sha256", track.file_integrity?.sha256],
            ["parser", track.mutagen_type],
            ["parser error", track.parser_error]
          ])}
          ${detailSection("Tags", [
            ["album artist", sources.tags?.album_artist ?? track.album_artist_tag],
            ["album artist repaired", sources.tags?.album_artist_repaired ?? track.album_artist_repaired],
            ["track artist", sources.tags?.track_artist ?? track.track_artist_tag],
            ["track artist repaired", sources.tags?.track_artist_repaired ?? track.track_artist_repaired],
            ["album", sources.tags?.album ?? track.tag_album],
            ["album repaired", sources.tags?.album_repaired ?? track.tag_album_repaired],
            ["title", sources.tags?.title ?? track.tag_title],
            ["title repaired", sources.tags?.title_repaired ?? track.tag_title_repaired],
            ["track", sources.tags?.track ?? track.tag_track],
            ["disc", sources.tags?.disc ?? track.tag_disc],
            ["date", sources.tags?.date ?? track.tag_date],
            ["genre", sources.tags?.genre ?? track.genre]
          ])}
          ${detailSection("Path / Filename / Audio ID", [
            ["path artist", sources.path?.artist],
            ["path album", sources.path?.album],
            ["path year", sources.path?.year],
            ["path source", sources.path?.source],
            ["filename artist", sources.filename?.artist],
            ["filename title", sources.filename?.title],
            ["filename track", sources.filename?.track_number],
            ["fingerprint algorithm", track.fingerprint?.algorithm],
            ["fingerprint duration", track.fingerprint?.duration_seconds],
            ["fingerprint warning", track.fingerprint?.warning],
            ["fingerprint error", track.fingerprint?.error],
            ["fingerprint", track.fingerprint?.value]
          ])}
        </div>
      `;
    }

    function detailRowHtml(track) {
      return `<tr class="details-row" data-details-index="${track._index}"><td colspan="9">${renderDetails(track)}</td></tr>`;
    }

    function toggleDetails(index, row) {
      const existing = rows.querySelector(`[data-details-index="${index}"]`);
      if (existing) {
        existing.remove();
        expandedRows.delete(index);
        return;
      }
      const track = trackByIndex.get(index);
      if (!track) return;
      row.insertAdjacentHTML("afterend", detailRowHtml(track));
      expandedRows.add(index);
    }

    function normalizeMatchText(value) {
      return String(value ?? "")
        .toLowerCase()
        .normalize("NFKD")
        .replace(/[\\u0300-\\u036f]/g, "")
        .replace(/\\b(feat|ft|featuring|remaster(ed)?|bonus|version|explicit|radio edit|album version|deluxe)\\b/g, " ")
        .replace(/[^\\p{L}\\p{N}]+/gu, " ")
        .trim();
    }

    function previewQuery(track) {
      return [track.artist, track.title].filter(Boolean).join(" ");
    }

    function previewScore(track, candidate) {
      const targetTitle = normalizeMatchText(track.title);
      const targetArtist = normalizeMatchText(track.artist);
      const targetAlbum = normalizeMatchText(track.album);
      const candidateTitle = normalizeMatchText(candidate.title);
      const candidateArtist = normalizeMatchText(candidate.artist);
      const candidateAlbum = normalizeMatchText(candidate.album);
      const candidateFullTitle = normalizeMatchText(candidate.fullTitle || candidate.title);
      let score = 0;
      if (candidateTitle === targetTitle) score += 4;
      else if (candidateTitle.includes(targetTitle) || targetTitle.includes(candidateTitle)) score += 2;
      if (candidateArtist === targetArtist) score += 3;
      else if (candidateArtist.includes(targetArtist) || targetArtist.includes(candidateArtist)) score += 1;
      if (targetAlbum && candidateAlbum && (candidateAlbum.includes(targetAlbum) || targetAlbum.includes(candidateAlbum))) score += 1;
      if (!/\\b(live|acoustic|remix|cover)\\b/.test(targetTitle) && /\\b(live|acoustic|remix|cover)\\b/.test(candidateFullTitle)) score -= 1.5;
      if (track.duration_seconds && candidate.duration) {
        const delta = Math.abs(Number(track.duration_seconds) - Number(candidate.duration));
        if (delta <= 3) score += 2;
        else if (delta <= 12) score += 1;
      }
      return score;
    }

    async function fetchJson(url) {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }

    function fetchJsonp(url, callbackParam = "callback") {
      return new Promise((resolve, reject) => {
        const callbackName = `musicArchiveJsonp_${Date.now()}_${Math.random().toString(36).slice(2)}`;
        const script = document.createElement("script");
        const cleanup = () => {
          delete window[callbackName];
          script.remove();
        };
        const timeout = window.setTimeout(() => {
          cleanup();
          reject(new Error("JSONP timeout"));
        }, 10000);
        window[callbackName] = data => {
          window.clearTimeout(timeout);
          cleanup();
          resolve(data);
        };
        const separator = url.includes("?") ? "&" : "?";
        script.src = `${url}${separator}${callbackParam}=${encodeURIComponent(callbackName)}`;
        script.onerror = () => {
          window.clearTimeout(timeout);
          cleanup();
          reject(new Error("JSONP request failed"));
        };
        document.head.appendChild(script);
      });
    }

    async function findDeezerPreview(track) {
      const url = `https://api.deezer.com/search/track?q=${encodeURIComponent(previewQuery(track))}&limit=8&output=jsonp`;
      const data = await fetchJsonp(url);
      const candidates = (data.data ?? [])
        .filter(item => item.preview)
        .map(item => ({
          source: "Deezer",
          title: item.title_short || item.title,
          fullTitle: item.title || item.title_short,
          artist: item.artist?.name || "",
          album: item.album?.title || "",
          duration: item.duration,
          previewUrl: item.preview,
          pageUrl: item.link
        }))
        .map(candidate => ({...candidate, score: previewScore(track, candidate)}))
        .sort((a, b) => b.score - a.score);
      return candidates.find(candidate => candidate.score >= 4) ?? null;
    }

    async function findItunesPreview(track) {
      const params = new URLSearchParams({
        term: previewQuery(track),
        media: "music",
        entity: "song",
        limit: "8"
      });
      const data = await fetchJson(`https://itunes.apple.com/search?${params}`);
      const candidates = (data.results ?? [])
        .filter(item => item.previewUrl)
        .map(item => ({
          source: "iTunes",
          title: item.trackName || "",
          fullTitle: item.trackName || "",
          artist: item.artistName || "",
          album: item.collectionName || "",
          duration: item.trackTimeMillis ? item.trackTimeMillis / 1000 : null,
          previewUrl: item.previewUrl,
          pageUrl: item.trackViewUrl
        }))
        .map(candidate => ({...candidate, score: previewScore(track, candidate)}))
        .sort((a, b) => b.score - a.score);
      return candidates.find(candidate => candidate.score >= 4) ?? null;
    }

    function formatTime(seconds) {
      if (!Number.isFinite(seconds)) return "0:00";
      seconds = Math.max(0, Math.floor(seconds));
      const minutes = Math.floor(seconds / 60);
      const rest = String(seconds % 60).padStart(2, "0");
      return `${minutes}:${rest}`;
    }

    function updatePlayerProgress() {
      if (player.classList.contains("error")) {
        playerProgress.style.width = "100%";
        return;
      }
      const duration = previewAudio.duration || 0;
      const current = previewAudio.currentTime || 0;
      const percent = duration ? Math.min(100, Math.max(0, current / duration * 100)) : 0;
      playerProgress.style.width = `${percent}%`;
      playerTime.textContent = duration ? `${formatTime(current)} / ${formatTime(duration)}` : formatTime(current);
      updateRowPreviewProgress(percent);
    }

    function updatePlayerToggle() {
      const hasError = player.classList.contains("error");
      playerToggle.innerHTML = hasError ? errorIcon : (previewAudio.paused ? playIcon : pauseIcon);
      playerToggle.setAttribute("aria-label", hasError ? "Preview error" : (previewAudio.paused ? "Play preview" : "Pause preview"));
      updateRowPreviewButtons();
    }

    function updateRowPreviewButtons() {
      rows.querySelectorAll("[data-preview-index]").forEach(button => {
        const isCurrent = Number(button.dataset.previewIndex) === currentPreviewIndex;
        const hasError = Number(button.dataset.previewIndex) === previewErrorIndex;
        const isPlaying = isCurrent && !previewAudio.paused && !previewAudio.ended;
        button.innerHTML = hasError ? errorIcon : (isPlaying ? pauseIcon : playIcon);
        button.classList.toggle("active", isCurrent);
        button.classList.toggle("error", hasError);
        button.style.setProperty("--preview-progress", isCurrent ? `${previewProgressPercent()}%` : "0%");
        button.setAttribute("aria-label", hasError ? "Preview not found" : (isPlaying ? "Pause preview" : "Play preview"));
      });
    }

    function previewProgressPercent() {
      const duration = previewAudio.duration || 0;
      if (!duration) return 0;
      return Math.min(100, Math.max(0, (previewAudio.currentTime || 0) / duration * 100));
    }

    function updateRowPreviewProgress(percent = previewProgressPercent()) {
      if (!currentPreviewIndex) return;
      const button = rows.querySelector(`[data-preview-index="${currentPreviewIndex}"]`);
      if (button) button.style.setProperty("--preview-progress", `${percent}%`);
    }

    async function togglePreview(index) {
      if (currentPreviewIndex === index && previewAudio.src) {
        if (previewAudio.paused) await previewAudio.play();
        else previewAudio.pause();
        updatePlayerToggle();
        return;
      }
      await playPreview(index);
    }

    async function playPreview(index) {
      const track = tracks[index - 1];
      if (!track) return;
      currentPreviewIndex = index;
      previewErrorIndex = null;
      player.hidden = false;
      player.classList.remove("error");
      playerTitle.textContent = `${track.artist} - ${track.title}`;
      playerStatus.textContent = "Searching Deezer...";
      playerProgress.style.width = "0%";
      playerTime.textContent = "0:00";
      updatePlayerToggle();
      previewAudio.removeAttribute("src");
      previewAudio.load();
      updatePlayerToggle();
      let candidate = null;
      try {
        candidate = await findDeezerPreview(track);
      } catch (error) {
        playerStatus.textContent = `Deezer failed: ${error.message}. Trying iTunes...`;
      }
      if (!candidate) {
        playerStatus.textContent = "Searching iTunes...";
        try {
          candidate = await findItunesPreview(track);
        } catch (error) {
          playerStatus.textContent = `iTunes failed: ${error.message}`;
        }
      }
      if (!candidate) {
        previewErrorIndex = index;
        player.classList.add("error");
        previewAudio.removeAttribute("src");
        previewAudio.load();
        playerStatus.textContent = "No suitable preview found";
        updatePlayerToggle();
        return;
      }
      playerStatus.textContent = `${candidate.source}: ${candidate.artist} - ${candidate.title}${candidate.album ? ` (${candidate.album})` : ""}`;
      previewAudio.src = candidate.previewUrl;
      try {
        await previewAudio.play();
      } catch {
        playerStatus.textContent += " · press play";
      }
      updatePlayerToggle();
    }

    function filtered() {
      const needle = q.value.trim().toLowerCase();
      const issueFilters = selectedIssues();
      return tracks.filter(track => {
        if (filters.artist && track.artist !== filters.artist) return false;
        if (filters.album && track.album !== filters.album) return false;
        if (issueFilters.length && !issueFilters.some(code => track._issues.includes(code))) return false;
        if (!needle) return true;
        return track._search.includes(needle);
      });
    }

    function render() {
      renderCombos();
      renderIssueFilter();
      const data = filtered().sort((a, b) => {
        const primary = collator.compare(String(sortValue(a, sortField)), String(sortValue(b, sortField))) * sortDirection;
        if (primary) return primary;
        return a._index - b._index;
      });
      renderSortIndicators();
      renderStats(data);
      rows.innerHTML = data.map(track => `
        <tr class="track-row" data-row-index="${track._index}">
          <td class="play-col">
            <button class="preview-button" type="button" data-preview-index="${track._index}" aria-label="Preview ${escapeHtml(track.artist)} - ${escapeHtml(track.title)}">${playIcon}</button>
          </td>
          <td class="num">${track._index}</td>
          <td class="num">${track.track_number ?? ""}</td>
          <td>${escapeHtml(track.artist)}</td>
          <td>${escapeHtml(track.album)}</td>
          <td>
            ${escapeHtml(track.title)}
            <div class="actions">
              <a href="${searchUrl(track, "spotify")}" target="_blank" rel="noreferrer">Spotify</a>
              <a href="${searchUrl(track, "youtube")}" target="_blank" rel="noreferrer">YouTube</a>
              <a href="${searchUrl(track, "musicbrainz")}" target="_blank" rel="noreferrer">MusicBrainz</a>
            </div>
          </td>
          <td>${renderIssues(track)}</td>
          <td class="optional">${track.year ?? ""}</td>
          <td class="optional"><div class="path">${escapeHtml(track.relative_path)}</div></td>
        </tr>
        ${expandedRows.has(track._index) ? detailRowHtml(track) : ""}
      `).join("");
      updateRowPreviewButtons();
    }

    document.querySelectorAll("th[data-sort]").forEach(th => th.addEventListener("click", () => {
      const next = th.dataset.sort;
      sortDirection = sortField === next ? -sortDirection : 1;
      sortField = next;
      render();
    }));
    q.addEventListener("input", render);
    rows.addEventListener("click", event => {
      const button = event.target.closest("[data-preview-index]");
      if (button) {
        togglePreview(Number(button.dataset.previewIndex));
        return;
      }
      const copyButton = event.target.closest("[data-copy-value]");
      if (copyButton) {
        navigator.clipboard?.writeText(copyButton.dataset.copyValue);
        copyButton.textContent = "Copied";
        window.setTimeout(() => { copyButton.textContent = "Copy"; }, 1200);
        return;
      }
      if (event.target.closest("a, button")) return;
      const row = event.target.closest("[data-row-index]");
      if (!row) return;
      const index = Number(row.dataset.rowIndex);
      toggleDetails(index, row);
    });
    playerToggle.addEventListener("click", () => {
      if (!previewAudio.src) return;
      if (previewAudio.paused) previewAudio.play();
      else previewAudio.pause();
      updatePlayerToggle();
    });
    playerSeek.addEventListener("click", event => {
      if (!previewAudio.duration) return;
      const rect = player.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
      previewAudio.currentTime = ratio * previewAudio.duration;
    });
    previewAudio.addEventListener("timeupdate", updatePlayerProgress);
    previewAudio.addEventListener("loadedmetadata", updatePlayerProgress);
    previewAudio.addEventListener("play", updatePlayerToggle);
    previewAudio.addEventListener("pause", updatePlayerToggle);
    previewAudio.addEventListener("ended", updatePlayerToggle);
    artistButton.addEventListener("click", () => toggleCombo("artist"));
    albumButton.addEventListener("click", () => toggleCombo("album"));
    artistSearch.addEventListener("input", () => renderCombo("artist"));
    albumSearch.addEventListener("input", () => renderCombo("album"));
    issueButton.addEventListener("click", () => {
      issuePanel.hidden = !issuePanel.hidden;
      artistPanel.hidden = true;
      albumPanel.hidden = true;
      renderIssueFilter();
    });
    document.addEventListener("click", event => {
      if (!issueFilter.contains(event.target) && !artistPanel.contains(event.target) && !artistButton.contains(event.target) && !albumPanel.contains(event.target) && !albumButton.contains(event.target)) {
        issuePanel.hidden = true;
        artistPanel.hidden = true;
        albumPanel.hidden = true;
        renderIssueFilter();
        renderCombos();
      }
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        issuePanel.hidden = true;
        artistPanel.hidden = true;
        albumPanel.hidden = true;
        renderIssueFilter();
        renderCombos();
      }
    });
    window.addEventListener("resize", updateHeaderHeight);
    loadData();
"""


def render_project_readme() -> str:
    return """# Music Archive

This directory contains a generated static archive for a local music library.

Run:

```sh
./serve.sh
```

Then open:

```text
http://127.0.0.1:8765/
```

Files:

- `music.json` is the machine-readable archive database.
- `index.html`, `app.js`, and `styles.css` are the local browser viewer.
- `Music.md` is the generated Markdown reading view.

The archive stores selected metadata, source metadata, SHA256 hashes, and Chromaprint fingerprints when `fpcalc` is available.
"""


def render_serve_script() -> str:
    return """#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8765}"

echo "Serving Music Archive at http://${HOST}:${PORT}/"
echo "Press Ctrl+C to stop."

exec python3 -m http.server "$PORT" --bind "$HOST"
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="music-archive",
        description="Archive metadata, hashes, fingerprints, and a browser viewer for a local music library.",
    )
    parser.add_argument(
        "music_dir",
        type=Path,
        help="Path to the local music library directory to scan.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("music-archive"),
        help="Output directory for music.json, static viewer files, and Music.md. Default: ./music-archive",
    )
    parser.add_argument(
        "--no-fingerprints",
        action="store_true",
        help="Skip Chromaprint fingerprint generation.",
    )
    parser.add_argument(
        "--no-hashes",
        action="store_true",
        help="Skip SHA256 file hashing.",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    music_root = args.music_dir.expanduser().resolve()
    if not music_root.is_dir():
        print(f"error: music directory does not exist: {music_root}", file=sys.stderr)
        return 2

    root = music_root.parent
    out_dir = args.output.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "music.json"

    tracks = scan_music(root, music_root)
    if not args.no_hashes:
        add_file_hashes(tracks, root, json_path)
    if not args.no_fingerprints:
        add_fingerprints(tracks, root, json_path)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "music_root": str(music_root),
        "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "file_hash_algorithm": "sha256",
        "track_count": len(tracks),
        "tracks": tracks,
    }

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "index.html").write_text(render_html(), encoding="utf-8")
    (out_dir / "styles.css").write_text(render_styles(), encoding="utf-8")
    (out_dir / "app.js").write_text(render_app_js(), encoding="utf-8")
    (out_dir / "Music.md").write_text(render_markdown(tracks), encoding="utf-8")
    (out_dir / "README.md").write_text(render_project_readme(), encoding="utf-8")
    serve_path = out_dir / "serve.sh"
    serve_path.write_text(render_serve_script(), encoding="utf-8")
    serve_path.chmod(0o755)

    print(f"tracks={len(tracks)}")
    print(f"json={json_path}")
    print(f"md={out_dir / 'Music.md'}")
    print(f"html={out_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
