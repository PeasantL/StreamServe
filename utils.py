"""Filesystem, ffmpeg and library-scanning helpers.

ffmpeg is driven through ``subprocess`` with an explicit argument list rather
than through ffmpeg-python: it removes a dependency, and it lets every
invocation carry a timeout, which the library did not make convenient.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import settings
from database import (
    add_videos_to_db,
    current_dir,
    list_videos,
    prune_missing,
    update_video_in_db,
)

log = logging.getLogger(__name__)

# What the catalogue serves directly. Both have a MIME type in main.MIME_TYPES
# and both play in a browser, so neither needs converting to be usable.
VIDEO_EXTENSIONS = (".mp4", ".webm")

# Containers that are converted to MP4 on scan. WebM is here despite being
# browser-playable because iOS refuses it, which is the reason this server
# transcodes at all; the rest simply do not play anywhere without help.
SOURCE_EXTENSIONS = (
    ".webm", ".mkv", ".mov", ".avi", ".m4v", ".flv", ".wmv", ".ts", ".mpg", ".mpeg", ".ogv",
)

# Codecs an MP4 container can hold and a browser can play. A source already in
# these needs remuxing, not re-encoding.
MP4_SAFE_VIDEO_CODECS = frozenset({"h264", "avc1"})
MP4_SAFE_AUDIO_CODECS = frozenset({"aac", "mp3"})

# The directory name is unchanged from when only WebM was archived here, so an
# existing library's archived originals are still found.
ORIGINALS_DIRNAME = "original_webm"
TIMECODE_RE = re.compile(r"^\d{1,2}:[0-5]\d:[0-5]\d(\.\d{1,3})?$")


class UnsafePathError(ValueError):
    """A user-supplied path escaped the directory it was meant to stay in."""


# --- path safety -------------------------------------------------------------


def resolve_within(base: Path, candidate: str | Path) -> Path:
    """Join *candidate* onto *base* and refuse anything that escapes it.

    Guards two distinct traps: ``Path`` and ``os.path.join`` both discard the
    left operand when the right one is absolute, and ``..`` segments walk up.
    """
    base_resolved = Path(base).resolve()
    target = (base_resolved / candidate).resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise UnsafePathError(f"{candidate!r} resolves outside {base_resolved}")
    return target


def video_path(video: dict[str, Any]) -> Path:
    """Absolute on-disk path for a database row, containment-checked."""
    directory = Path(video.get("directory") or current_dir())
    return resolve_within(directory, video["path"])


def thumbnail_path(video_id: str) -> Path:
    return resolve_within(settings.thumbnail_dir, f"{video_id}.jpg")


def get_originals_dir(directory: Path | None = None) -> Path:
    """Where a converted file's untouched original is kept."""
    return Path(directory or current_dir()) / ORIGINALS_DIRNAME


def get_unique_filename(original_filename: str, directory: Path) -> str:
    """Append a counter until the name is free in *directory*."""
    stem = Path(original_filename).stem
    suffix = Path(original_filename).suffix
    filename = f"{stem}{suffix}"
    counter = 1
    while (Path(directory) / filename).exists():
        filename = f"{stem}_{counter}{suffix}"
        counter += 1
    return filename


def get_sibling_folders(directory: Path | None = None) -> list[str]:
    """Names of the directories offered as switchable libraries."""
    directory = Path(directory or current_dir())
    parent = settings.parent_dir.resolve()
    try:
        return sorted(
            child.name
            for child in parent.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and child.resolve().parent == parent
            and child.resolve() != directory.resolve()
        )
    except OSError as exc:
        log.warning("Cannot list sibling folders of %s: %s", parent, exc)
        return []


def library_dir(folder: str | None = None) -> Path:
    """Resolve a named library to a real direct child of the media parent."""
    if folder is None:
        return current_dir()
    if not folder or folder in (".", "..") or Path(folder).name != folder:
        raise UnsafePathError("Invalid folder name")
    parent = settings.parent_dir.resolve()
    child = parent / folder
    if child.is_symlink() or not child.is_dir():
        raise UnsafePathError("Folder not found")
    resolved = child.resolve()
    if resolved.parent != parent:
        raise UnsafePathError("Folder is outside the library parent")
    return resolved


# --- subtitles ---------------------------------------------------------------

# Browsers play WebVTT and nothing else through <track>. SubRip is what people
# actually have on disk, so it is converted on the way out.
SUBTITLE_EXTENSIONS = (".vtt", ".srt")

# "00:00:01,000 --> 00:00:04,000", the one line where SubRip and WebVTT differ
# in a way that matters: WebVTT wants a decimal point, not a comma.
_SRT_TIMECODE_RE = re.compile(
    r"^(\d{1,3}:\d{2}:\d{2}),(\d{1,3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}),(\d{1,3})(.*)$"
)


def subtitle_path(video: dict[str, Any]) -> Path | None:
    """A sidecar subtitle file sitting next to the video, if there is one.

    ``.vtt`` wins over ``.srt`` when both exist: it needs no conversion, so
    whatever it contains is what the browser will actually see.
    """
    try:
        source = video_path(video)
    except UnsafePathError:
        return None

    for extension in SUBTITLE_EXTENSIONS:
        try:
            candidate = resolve_within(source.parent, f"{source.stem}{extension}")
        except UnsafePathError:
            continue
        if candidate.is_file():
            return candidate
    return None


def to_webvtt(text: str) -> str:
    """Convert SubRip to WebVTT, or pass WebVTT through unchanged.

    Only two things have to change for a browser to accept the result: the
    ``WEBVTT`` header, and the comma before the milliseconds. Cue text is left
    exactly as it was -- rewriting it would be a good way to lose formatting
    for no benefit.
    """
    if text.lstrip("\ufeff").lstrip().startswith("WEBVTT"):
        return text.lstrip("\ufeff")

    lines = []
    for line in text.lstrip("\ufeff").splitlines():
        match = _SRT_TIMECODE_RE.match(line.strip())
        if match:
            start, start_ms, end, end_ms, rest = match.groups()
            lines.append(f"{start}.{start_ms} --> {end}.{end_ms}{rest}")
        else:
            lines.append(line)

    return "WEBVTT\n\n" + "\n".join(lines)


# --- content hashing ---------------------------------------------------------

DIGEST_CHUNK_SIZE = 1024 * 1024


def file_digest(path: Path) -> str | None:
    """SHA-256 of a file's bytes, or None when it cannot be read.

    Read in chunks: these are video files, and loading one into memory to hash
    it would cost more than the duplicate check saves.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(DIGEST_CHUNK_SIZE):
                digest.update(chunk)
    except OSError as exc:
        log.warning("Could not hash %s: %s", path, exc)
        return None
    return digest.hexdigest()


def find_duplicate_groups(directory: Path) -> list[list[dict[str, Any]]]:
    """Rows in *directory* holding byte-identical files, grouped.

    Sizes are compared first and only files whose size collides are hashed.
    Hashing a whole library would mean reading every byte of every video on
    every scan; a size collision between different videos is rare enough that
    this is normally close to free.
    """
    by_size: dict[int, list[dict[str, Any]]] = {}
    for row in list_videos(directory):
        source = Path(directory) / row["path"]
        if not source.exists():
            continue
        size = row.get("size_bytes") or source.stat().st_size
        by_size.setdefault(size, []).append(row)

    groups = []
    for candidates in by_size.values():
        if len(candidates) < 2:
            continue

        by_hash: dict[str, list[dict[str, Any]]] = {}
        for row in candidates:
            digest = row.get("content_hash") or file_digest(Path(directory) / row["path"])
            if digest is None:
                continue
            if row.get("content_hash") != digest:
                update_video_in_db(row["id"], {"content_hash": digest}, directory)
            by_hash.setdefault(digest, []).append(row)

        groups.extend(rows for rows in by_hash.values() if len(rows) > 1)

    return groups


# --- ffmpeg ------------------------------------------------------------------


@dataclass(frozen=True)
class MediaInfo:
    """What one ffprobe call tells us about a file.

    Every field is optional because ffprobe reports what a container happens
    to carry: a stream copy from a source with no duration header, or a probe
    that timed out, leaves gaps rather than failing the import.
    """

    has_audio: bool = False
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    size_bytes: int | None = None

    def as_row_fields(self) -> dict[str, Any]:
        """The subset stored on a database row."""
        return {
            "has_audio": self.has_audio,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "size_bytes": self.size_bytes,
        }


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # ffprobe reports "N/A" as nan for some containers.
    return result if result == result and result >= 0 else None


def probe_media(path: Path) -> MediaInfo:
    """Read duration, dimensions, codecs and size in a single ffprobe call.

    This used to be ``has_audio_stream``, which paid the full cost of spawning
    ffprobe and then discarded everything except whether an audio stream
    existed. The same invocation with ``-show_format -show_streams`` returns
    the rest for free, which is what the duration badges, the resolution
    labels and the remux decision in ``convert_to_mp4`` are built on.

    A failure is not fatal: an unreadable file yields an empty ``MediaInfo``
    and the video is still catalogued.
    """
    command = [
        "ffprobe",
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=settings.ffmpeg_timeout,
            check=True,
        )
        data = json.loads(result.stdout or b"{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        log.warning("ffprobe failed for %s: %s", path, exc)
        return MediaInfo()

    if not isinstance(data, dict):
        return MediaInfo()

    streams = [item for item in data.get("streams") or [] if isinstance(item, dict)]
    container = data.get("format") if isinstance(data.get("format"), dict) else {}

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # Duration lives on the container for MP4 and on the video stream for some
    # WebM files, so fall back rather than reporting an unknown length.
    duration = _as_float(container.get("duration"))
    if duration is None and video is not None:
        duration = _as_float(video.get("duration"))

    return MediaInfo(
        has_audio=audio is not None,
        duration=duration,
        width=_as_int(video.get("width")) if video else None,
        height=_as_int(video.get("height")) if video else None,
        video_codec=str(video.get("codec_name")) if video and video.get("codec_name") else None,
        audio_codec=str(audio.get("codec_name")) if audio and audio.get("codec_name") else None,
        size_bytes=_as_int(container.get("size")),
    )


def has_audio_stream(path: Path) -> bool:
    """True when the file carries at least one audio stream."""
    return probe_media(path).has_audio


def generate_thumbnail(source: Path, destination: Path, timestamp: str = "00:00:01") -> Path:
    """Write a JPEG thumbnail for *source* at *timestamp*.

    The frame is rendered to a temporary file and moved into place only on
    success, so a failure never destroys the thumbnail that was already there.
    Raises ``subprocess.SubprocessError`` on failure rather than returning None,
    so callers cannot mistake a failure for success.
    """
    if not TIMECODE_RE.match(timestamp):
        raise ValueError(f"Invalid timecode {timestamp!r}; expected HH:MM:SS")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # delete=False because ffmpeg writes the file and it is renamed into place.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        dir=destination.parent,
        prefix=f".{destination.stem}.",
        suffix=".jpg",
        delete=False,
    )
    handle.close()
    temp_path = Path(handle.name)

    command = [
        "ffmpeg",
        "-ss", timestamp,
        "-i", str(source),
        "-vf", f"scale='min({settings.thumbnail_width},iw)':-2",
        "-frames:v", "1",
        "-qscale:v", "4",
        "-y",
        "-loglevel", "error",
        "-f", "image2",
        str(temp_path),
    ]

    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=settings.ffmpeg_timeout,
            check=True,
        )
        if not temp_path.exists() or temp_path.stat().st_size == 0:
            raise subprocess.SubprocessError(
                f"ffmpeg produced no frame for {source} at {timestamp}"
            )
        os.replace(temp_path, destination)
        return destination
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def plan_conversion(info: MediaInfo) -> tuple[list[str], bool]:
    """Codec arguments for converting a file described by *info*.

    Each stream is decided separately, because they fail independently: an
    MKV commonly holds H.264 video that an MP4 can carry untouched alongside
    an audio track that it cannot. Copying the video and re-encoding only the
    audio turns a minutes-long transcode into a seconds-long remux.

    Returns the arguments and whether any stream is actually being encoded,
    which is what decides if the hardware encoder settings are worth applying.
    """
    video_codec = (info.video_codec or "").lower()
    audio_codec = (info.audio_codec or "").lower()

    # An unreadable probe reports no video codec at all. Re-encoding is the
    # safe answer: a copy of a stream MP4 cannot hold produces a file that
    # fails to play, and it fails silently.
    copy_video = video_codec in MP4_SAFE_VIDEO_CODECS
    copy_audio = not info.has_audio or audio_codec in MP4_SAFE_AUDIO_CODECS

    args = ["-c:v", "copy" if copy_video else settings.video_encoder]
    if info.has_audio:
        args += ["-c:a", "copy" if copy_audio else "aac"]

    return args, not (copy_video and copy_audio)


def convert_to_mp4(source: Path, destination: Path, info: MediaInfo | None = None) -> Path:
    """Produce a browser-playable MP4, remuxing when the codecs already allow.

    Writes to a temp file and moves it into place, so an interrupted run never
    leaves a half-written video where a playable one is expected.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.partial.mp4")

    info = probe_media(source) if info is None else info
    codec_args, encoding = plan_conversion(info)

    command = ["ffmpeg"]
    # Hardware decode helps only when frames are actually being re-encoded; on
    # a stream copy it costs a setup round-trip and buys nothing.
    if encoding and settings.hwaccel:
        command += ["-hwaccel", settings.hwaccel]
    command += ["-i", str(source)]
    command += codec_args
    command += [
        # Moves the index to the front so playback can start before the whole
        # file has arrived, which is the point of streaming it.
        "-movflags", "+faststart",
        "-y",
        "-loglevel", "error",
        str(temp_path),
    ]

    log.info(
        "Converting %s (%s/%s): %s",
        source.name,
        info.video_codec or "unknown",
        info.audio_codec or "none",
        "re-encoding" if encoding else "remuxing",
    )

    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=settings.convert_timeout,
            check=True,
        )
        os.replace(temp_path, destination)
        return destination
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


# --- library scanning --------------------------------------------------------


def _new_row(directory: Path, filename: str, *, source_name: str | None = None) -> dict[str, Any]:
    path = Path(directory) / filename
    video_id = str(uuid.uuid4())
    created = datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    return {
        "id": video_id,
        "directory": str(Path(directory).resolve()),
        "original_filename": source_name or filename,
        "title": Path(source_name or filename).stem,
        "path": filename,
        "thumbnail_path": f"{video_id}.jpg",
        "creation_date": created,
        "description": "",
        "tags": [],
        **probe_media(path).as_row_fields(),
        # Recorded so a re-scan can tell that this WebM was already converted.
        # The previous check compared against a freshly generated UUID and so
        # could never be true.
        "source_name": source_name,
    }


def _ensure_thumbnail(video_id: str, source: Path) -> None:
    destination = thumbnail_path(video_id)
    if destination.exists():
        return
    try:
        generate_thumbnail(source, destination)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        log.warning("Thumbnail generation failed for %s: %s", source, exc)


def convert_source_files(directory: Path) -> int:
    """Convert loose non-MP4 files and archive the untouched originals.

    Runs *before* the catalogue scan so a source file is never registered
    under a path that conversion is about to move away.
    """
    directory = Path(directory)
    archive = get_originals_dir(directory)
    converted = 0

    already_converted = {
        row["source_name"]
        for row in list_videos(directory)
        if row.get("source_name") and (directory / row["path"]).exists()
    }

    for entry in sorted(directory.iterdir()):
        if not entry.is_file() or entry.suffix.lower() not in SOURCE_EXTENSIONS:
            continue

        archive.mkdir(parents=True, exist_ok=True)

        if entry.name in already_converted:
            # Converted on an earlier run; just move the source out of the way.
            shutil.move(str(entry), archive / get_unique_filename(entry.name, archive))
            continue

        mp4_name = f"{uuid.uuid4()}.mp4"
        try:
            convert_to_mp4(entry, directory / mp4_name)
        except (subprocess.SubprocessError, OSError) as exc:
            log.error("Conversion failed for %s: %s", entry, exc)
            continue

        row = _new_row(directory, mp4_name, source_name=entry.name)
        archived = get_unique_filename(entry.name, archive)
        shutil.move(str(entry), archive / archived)
        # The field is named for the days when only WebM was archived. It now
        # holds whatever container the original arrived in.
        row["original_webm"] = archived

        add_videos_to_db([row])
        _ensure_thumbnail(row["id"], directory / mp4_name)
        converted += 1

    return converted


# Present on every row written since media probing was introduced. Its absence
# is what distinguishes a row that predates the feature from one whose probe
# genuinely found nothing.
_PROBE_MARKER = "video_codec"


def backfill_media_info(directory: Path) -> int:
    """Probe rows catalogued before duration and size were recorded.

    Without this an existing library would show no badges until every file was
    re-added by hand. Rows already carrying the fields are skipped, so the
    cost is paid once rather than on every scan.
    """
    filled = 0
    for row in list_videos(directory):
        if _PROBE_MARKER in row:
            continue
        source = Path(directory) / row["path"]
        if not source.exists():
            continue
        update_video_in_db(row["id"], probe_media(source).as_row_fields(), directory)
        filled += 1

    if filled:
        log.info("Backfilled media info for %d row(s) in %s", filled, directory)
    return filled


def scan_library(directory: Path | None = None) -> dict[str, int]:
    """Bring the database in line with what is on disk for one directory."""
    directory = Path(directory or current_dir())
    if not directory.is_dir():
        log.warning("Cannot scan %s: not a directory", directory)
        return {"converted": 0, "added": 0, "pruned": 0, "backfilled": 0, "duplicates": 0}

    converted = convert_source_files(directory)

    on_disk = {
        entry.name
        for entry in directory.iterdir()
        if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS
    }
    known = {row["path"] for row in list_videos(directory)}

    new_rows = [_new_row(directory, name) for name in sorted(on_disk - known)]
    add_videos_to_db(new_rows)

    # Rows whose file has gone are removed rather than filtered out on every
    # read, so they stop accumulating invisibly.
    pruned = prune_missing(directory, on_disk)

    for row in list_videos(directory):
        source = directory / row["path"]
        if source.exists():
            _ensure_thumbnail(row["id"], source)

    filled = backfill_media_info(directory)

    # Reported, never acted on. Two copies of a video can be deliberate, and
    # deleting one on the user's behalf is not a decision a scan should make.
    duplicate_groups = find_duplicate_groups(directory)
    for group in duplicate_groups:
        log.info(
            "Duplicate content in %s: %s",
            directory, ", ".join(row.get("title") or row["id"] for row in group),
        )

    log.info(
        "Scanned %s: %d converted, %d added, %d pruned, %d backfilled, %d duplicate group(s)",
        directory, converted, len(new_rows), pruned, filled, len(duplicate_groups),
    )
    return {
        "converted": converted,
        "added": len(new_rows),
        "pruned": pruned,
        "backfilled": filled,
        "duplicates": sum(len(group) - 1 for group in duplicate_groups),
    }


def matches_query(row: dict[str, Any], query: str) -> bool:
    """True when *query* appears in the row's title, description or tags.

    Matching is case-insensitive substring, across every field a user might
    remember the video by. Tags match as substrings too, so searching "hair"
    finds ``blue_hair`` without the user having to know the exact tag.
    """
    needle = query.casefold()
    if needle in str(row.get("title") or "").casefold():
        return True
    if needle in str(row.get("description") or "").casefold():
        return True
    return any(needle in str(tag).casefold() for tag in row.get("tags") or ())


def matches_tags(row: dict[str, Any], tags: Sequence[str]) -> bool:
    """True when the row carries *every* requested tag.

    Conjunctive rather than disjunctive: selecting a second tag should narrow
    the grid, which is what makes clicking through tags a way to find one
    video rather than a way to widen the result set.
    """
    present = {str(tag).casefold() for tag in row.get("tags") or ()}
    return all(tag.casefold() in present for tag in tags)


def collect_tags(directory: Path | None = None) -> list[tuple[str, int]]:
    """Every tag in *directory* with its use count, most-used first."""
    counts: Counter[str] = Counter()
    for row in list_videos(directory or current_dir()):
        counts.update(str(tag).casefold() for tag in row.get("tags") or ())
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def format_duration(seconds: float | None) -> str:
    """``H:MM:SS`` past an hour, ``M:SS`` below it. Empty when unknown."""
    if seconds is None or seconds < 0:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_size(size_bytes: int | None) -> str:
    """Human-readable file size. Empty when unknown."""
    if size_bytes is None or size_bytes < 0:
        return ""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def format_resolution(width: int | None, height: int | None) -> str:
    """The shorthand people actually read: 1080p, 720p, and so on.

    Quality labels like "1080p" name the short side of the frame, not
    whichever dimension ffprobe happens to call "height". A portrait video
    is taller than it is wide, so reading height alone would label a
    1080x1920 phone clip "1920p" -- overstating it by a full tier.
    """
    if not width or not height:
        return f"{height}p" if height else ""
    return f"{min(width, height)}p"


def format_views(count: int | None) -> str:
    """A view badge, or "" for a video nobody has opened yet.

    An unwatched library would otherwise wear "0 views" on every tile, which
    is noise: the absence of a badge says the same thing more quietly.
    """
    if not count or count < 1:
        return ""
    return "1 view" if count == 1 else f"{count} views"


@dataclass(frozen=True)
class VideoPage:
    """One page of the catalogue, plus what is needed to page through it."""

    videos: list[dict[str, Any]]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, -(-self.total // self.page_size))

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def first_index(self) -> int:
        """1-based index of the first item shown, for "x-y of z"."""
        return 0 if not self.total else (self.page - 1) * self.page_size + 1

    @property
    def last_index(self) -> int:
        return min(self.page * self.page_size, self.total)


def _sort_key(sort_by: str):
    """(key, reverse) for one of the supported orderings.

    A row that has never been opened counts as zero views, so it sorts to the
    end of a most-viewed ordering rather than to the front.
    """
    if sort_by == "most_viewed":
        return (lambda row: row.get("view_count") or 0), True
    return (lambda row: str(row.get("creation_date") or "")), True


def _view_model(row: dict[str, Any]) -> dict[str, Any]:
    """The shape the template renders. Built per *page*, not per library."""
    has_thumbnail = thumbnail_path(row["id"]).exists()
    return {
        "id": row["id"],
        "title": row.get("title") or Path(row["path"]).stem,
        "path": row["path"],
        "thumbnail": f"/thumbnails/{row['id']}.jpg" if has_thumbnail else None,
        "has_thumbnail": has_thumbnail,
        "has_audio": row.get("has_audio", True),
        "duration": row.get("duration"),
        "duration_label": format_duration(row.get("duration")),
        "resolution_label": format_resolution(row.get("width"), row.get("height")),
        "size_bytes": row.get("size_bytes"),
        "size_label": format_size(row.get("size_bytes")),
        "view_count": row.get("view_count") or 0,
        "views_label": format_views(row.get("view_count")),
        "creation_date": row.get("creation_date") or "",
        "description": row.get("description", ""),
        "tags": row.get("tags", []),
    }


def ordered_rows(
    sort_by: str = "newest",
    directory: Path | None = None,
    query: str = "",
    tags: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Database rows matching the filter, in display order.

    Shared by the grid and by neighbour lookup so that "next video" means the
    next one the user can actually see, under whatever sort and filter the
    grid was showing.
    """
    directory = Path(directory or current_dir())
    query = query.strip()

    matched = []
    for row in list_videos(directory):
        if query and not matches_query(row, query):
            continue
        if tags and not matches_tags(row, tags):
            continue
        try:
            source = video_path(row)
        except UnsafePathError:
            log.error("Skipping row %s with unsafe path %r", row.get("id"), row.get("path"))
            continue
        if not source.exists():
            continue
        matched.append(row)

    key, reverse = _sort_key(sort_by)
    matched.sort(key=key, reverse=reverse)
    return matched


def find_neighbours(
    video_id: str,
    sort_by: str = "newest",
    directory: Path | None = None,
    query: str = "",
    tags: Sequence[str] = (),
) -> tuple[str | None, str | None]:
    """The ids either side of *video_id* in display order.

    Returns (None, None) when the video is not in the current view at all --
    it was deleted, or the filter excludes it -- rather than guessing at a
    position it does not occupy.
    """
    rows = ordered_rows(sort_by, directory, query, tags)
    index = next((i for i, row in enumerate(rows) if row["id"] == video_id), None)
    if index is None:
        return None, None

    previous = rows[index - 1]["id"] if index > 0 else None
    following = rows[index + 1]["id"] if index + 1 < len(rows) else None
    return previous, following


def browse_videos(
    sort_by: str = "newest",
    directory: Path | None = None,
    query: str = "",
    tags: Sequence[str] = (),
    page: int = 1,
    page_size: int | None = None,
) -> VideoPage:
    """Filter, sort and page the catalogue for one directory.

    Rows are filtered and sorted first and only the surviving page is turned
    into a view model, so the per-tile thumbnail stat is paid for the videos
    actually rendered rather than for the whole library. The file-existence
    check cannot be deferred the same way: it decides whether a row counts at
    all, and a total that included missing files would page to empty screens.
    """
    matched = ordered_rows(sort_by, directory, query, tags)
    page_size = settings.page_size if page_size is None else page_size
    total = len(matched)
    if page_size <= 0:
        return VideoPage([_view_model(row) for row in matched], total, 1, max(total, 1))

    # Clamped rather than 404: a deletion can shrink the library under a
    # bookmarked page number, and an empty screen is a worse answer than the
    # last real page.
    pages = max(1, -(-total // page_size))
    page = min(max(page, 1), pages)
    start = (page - 1) * page_size

    return VideoPage(
        videos=[_view_model(row) for row in matched[start:start + page_size]],
        total=total,
        page=page,
        page_size=page_size,
    )


def get_video_files(
    sort_by: str = "newest",
    directory: Path | None = None,
    query: str = "",
    tags: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Every matching row for a directory, unpaged."""
    return browse_videos(
        sort_by=sort_by, directory=directory, query=query, tags=tags, page_size=0
    ).videos
