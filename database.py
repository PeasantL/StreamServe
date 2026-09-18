"""Persistence for the video catalogue.

The store is still a single JSON document, but with the two properties the
previous version lacked:

  * **Atomic writes.** Every save goes to a sibling temp file and is moved into
    place with ``os.replace``, which is atomic on POSIX. A crash or a full disk
    can no longer leave a half-written file that fails to parse.
  * **Serialised access.** A reentrant lock guards read-modify-write, so two
    concurrent requests can no longer lose one of the two updates. The parsed
    document is cached in memory, so a mutation is one write rather than a
    read plus a write.

Rows are scoped by the directory they were found in, so switching folders no
longer lets a row from one library resolve against a same-named file in
another.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from config import settings

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def _empty_db() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "current_dir": str(settings.video_dir.resolve()),
        "videos": [],
    }


def _migrate(db: dict[str, Any]) -> dict[str, Any]:
    """Bring a v1 document (flat list, no directory scoping) up to v2."""
    if db.get("version") == SCHEMA_VERSION:
        db.setdefault("current_dir", str(settings.video_dir.resolve()))
        db.setdefault("videos", [])
        return db

    videos = db.get("videos", [])
    home = str(settings.video_dir.resolve())
    for video in videos:
        # v1 rows carry a bare filename and no directory. The only directory we
        # can honestly attribute them to is the one configured at migration
        # time; rows whose file is not there will be pruned on the next scan.
        video.setdefault("directory", home)
    log.info("Migrated %d rows to schema v%d", len(videos), SCHEMA_VERSION)
    return {"version": SCHEMA_VERSION, "current_dir": home, "videos": videos}


def _read_from_disk() -> dict[str, Any]:
    try:
        with settings.db_file.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return _empty_db()
    except json.JSONDecodeError:
        log.error("Database %s is corrupt; starting from empty", settings.db_file)
        return _empty_db()

    if not isinstance(data, dict):
        log.error("Database %s has unexpected shape; starting from empty", settings.db_file)
        return _empty_db()
    return _migrate(data)


def _write_to_disk(db: dict[str, Any]) -> None:
    """Serialise atomically: write a temp file in the same directory, rename."""
    target = settings.db_file
    target.parent.mkdir(parents=True, exist_ok=True)

    # delete=False because the file is renamed into place, not closed away.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(db, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except BaseException:
        # Never leave the temp file behind on failure.
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise


def init_db() -> None:
    """Load the database into memory, creating it on disk if absent."""
    with _lock:
        global _cache
        _cache = _read_from_disk()
        if not settings.db_file.exists():
            _write_to_disk(_cache)


def load_db() -> dict[str, Any]:
    """Return the in-memory document, loading it on first use."""
    with _lock:
        global _cache
        if _cache is None:
            _cache = _read_from_disk()
        return _cache


def save_db(db: dict[str, Any] | None = None) -> None:
    with _lock:
        global _cache
        if db is not None:
            _cache = db
        if _cache is not None:
            _write_to_disk(_cache)


def reset_cache() -> None:
    """Drop the in-memory copy. Used by tests and after a settings change."""
    with _lock:
        global _cache
        _cache = None


# --- current directory -------------------------------------------------------
#
# Which folder the UI is browsing is runtime state, so it lives here rather than
# in config.json. A VIDEO_DIR environment variable or config entry supplies the
# default; a stored selection wins only while it remains a real directory under
# the same parent.


def current_dir() -> Path:
    with _lock:
        stored = load_db().get("current_dir")
        if stored:
            candidate = Path(stored)
            configured_parent = settings.parent_dir.resolve()
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if resolved.is_dir() and resolved.parent == configured_parent:
                return resolved
        return settings.video_dir.resolve()


def set_current_dir(path: Path) -> None:
    with _lock:
        global _cache
        db = load_db()
        updated = {**db, "current_dir": str(Path(path).resolve())}
        # Publish the selection only after it is durable on disk.
        _write_to_disk(updated)
        _cache = updated


# --- video rows --------------------------------------------------------------


def _rows_for(directory: Path) -> Iterator[dict[str, Any]]:
    key = str(Path(directory).resolve())
    for video in load_db()["videos"]:
        if video.get("directory") == key:
            yield video


def list_videos(directory: Path | None = None) -> list[dict[str, Any]]:
    with _lock:
        return list(_rows_for(directory or current_dir()))


def get_video_by_id(video_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    """Look up a row, scoped to one directory so IDs cannot cross libraries."""
    with _lock:
        for video in _rows_for(directory or current_dir()):
            if video["id"] == video_id:
                return video
        return None


def find_duplicate(
    directory: Path | None = None,
    *,
    source_url: str | None = None,
    page_url: str | None = None,
    source_hash: str | None = None,
) -> dict[str, Any] | None:
    """First row in *directory* matching any of the identities given.

    Three separate ways the same video can arrive twice: the same direct file
    URL, the same booru post page, or different URLs that turn out to serve
    byte-identical files. Any one of them is enough to call it a duplicate.
    """
    with _lock:
        for video in _rows_for(directory or current_dir()):
            if source_url and video.get("source_url") == source_url:
                return video
            if page_url and video.get("description") == page_url:
                return video
            if source_hash and video.get("source_hash") == source_hash:
                return video
        return None


def add_video_to_db(video_data: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        video_data.setdefault("directory", str(current_dir()))
        db = load_db()
        db["videos"].append(video_data)
        save_db()
        return video_data


def add_videos_to_db(rows: list[dict[str, Any]]) -> None:
    """Append many rows in one write, for bulk scans."""
    if not rows:
        return
    with _lock:
        db = load_db()
        for row in rows:
            row.setdefault("directory", str(current_dir()))
            db["videos"].append(row)
        save_db()


def update_video_in_db(
    video_id: str, updated_data: dict[str, Any], directory: Path | None = None
) -> dict[str, Any] | None:
    with _lock:
        video = get_video_by_id(video_id, directory)
        if video is None:
            return None
        # 'id' and 'directory' identify the row; callers may not rewrite them.
        for key, value in updated_data.items():
            if key not in ("id", "directory"):
                video[key] = value
        save_db()
        return video


def record_view(video_id: str, directory: Path | None = None) -> int | None:
    """Count one viewing of *video_id*, returning the new total.

    Read-modify-write under the same lock as every other mutation, so two
    people opening the same video at once cannot both read 4 and both write 5.
    Rows catalogued before view counting existed have no ``view_count``; the
    first view starts them at 1 rather than needing a migration.
    """
    with _lock:
        video = get_video_by_id(video_id, directory)
        if video is None:
            return None
        count = video.get("view_count")
        video["view_count"] = (count if isinstance(count, int) else 0) + 1
        save_db()
        return video["view_count"]


def delete_video_from_db(video_id: str, directory: Path | None = None) -> bool:
    with _lock:
        key = str(Path(directory or current_dir()).resolve())
        db = load_db()
        for index, video in enumerate(db["videos"]):
            if video["id"] == video_id and video.get("directory") == key:
                del db["videos"][index]
                save_db()
                return True
        return False


def prune_missing(directory: Path, existing_paths: set[str]) -> int:
    """Drop rows in *directory* whose file is gone. Other libraries untouched."""
    with _lock:
        key = str(Path(directory).resolve())
        db = load_db()
        before = len(db["videos"])
        db["videos"] = [
            video
            for video in db["videos"]
            if video.get("directory") != key or video.get("path") in existing_paths
        ]
        removed = before - len(db["videos"])
        if removed:
            log.info("Pruned %d row(s) with no file in %s", removed, key)
            save_db()
        return removed
