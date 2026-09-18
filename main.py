"""Video catalogue and streaming server."""

from __future__ import annotations

import datetime
import logging
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined
from pydantic import BaseModel, Field, field_validator

import auth
import boorus
import database
import downloads
import utils
from config import settings
from middleware import client_ip, whitelist_middleware
from ranges import RangeNotSatisfiable, parse_range
from tasks import registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("streamserve")

MIME_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
STREAM_CHUNK_SIZE = 1024 * 1024

# Scans are serialised because conversion and catalogue changes share files.
# A request for the same folder reuses the task already waiting or running.
_scan_lock = threading.Lock()
_scan_tasks_lock = threading.Lock()
_scan_tasks: dict[Path, str] = {}


def _run_scan(task_id: str, directory: Path) -> None:
    """Scan a library in a worker thread, reporting through the task registry."""
    try:
        with _scan_lock:
            registry.update(task_id, status="scanning", progress=10)
            result = utils.scan_library(directory)
            registry.update(task_id, status="completed", progress=100, error=None)
            log.info("Scan of %s finished: %s", directory, result)
    except Exception as exc:  # noqa: BLE001 - surfaced to the client
        log.exception("Library scan of %s failed", directory)
        registry.update(task_id, status="failed", error=str(exc))
    finally:
        with _scan_tasks_lock:
            if _scan_tasks.get(directory) == task_id:
                del _scan_tasks[directory]


def _schedule_scan(directory: Path) -> str:
    with _scan_tasks_lock:
        existing = _scan_tasks.get(directory)
        if existing is not None:
            return existing
        task_id = registry.create("scan")
        registry.update(task_id, status="queued")
        _scan_tasks[directory] = task_id
        try:
            threading.Thread(target=_run_scan, args=(task_id, directory), daemon=True).start()
        except RuntimeError:
            # Selection has already been saved. Report a failed scan task but
            # still let the client navigate to its selected folder.
            del _scan_tasks[directory]
            registry.update(task_id, status="failed", error="Could not start scan")
        return task_id


def _scan_task_for(directory: Path) -> str | None:
    with _scan_tasks_lock:
        return _scan_tasks.get(directory)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)
    database.init_db()

    # Transcoding on the event loop would stall every in-flight stream, so the
    # initial scan runs on a worker thread and the server starts serving now.
    _schedule_scan(database.current_dir())

    yield


app = FastAPI(title="StreamServe", lifespan=lifespan)

# Registration order is reversed at request time: the last middleware added is
# the outermost and runs first. The allowlist is added last so it stays the
# outer gate -- an address that is not permitted at all should be turned away
# without the password layer ever looking at it.
app.middleware("http")(auth.auth_middleware)
app.middleware("http")(whitelist_middleware)

# No CORS middleware: the UI is served from this same origin, and the previous
# allow_origins=["*"] with allow_credentials=True let any site a whitelisted
# user visited call these unauthenticated POST and DELETE routes.

templates = Jinja2Templates(directory="templates")
# Surfaces a template variable typo as an error instead of rendering an empty
# string, which is how the player title silently went missing.
templates.env.undefined = StrictUndefined


def _library_or_404(folder: str | None) -> Path:
    try:
        return utils.library_dir(folder)
    except (utils.UnsafePathError, OSError):
        raise HTTPException(status_code=404, detail="Folder not found") from None


def _get_video_or_404(video_id: str, directory: Path | None = None) -> dict[str, Any]:
    video = database.get_video_by_id(video_id, directory)
    if not video:
        raise HTTPException(status_code=404, detail=f"Video with ID '{video_id}' not found.")
    return video


def _resolved_video_path(video: dict[str, Any]) -> Path:
    """Containment-checked path for a row, 404 rather than leaking the reason."""
    try:
        path = utils.video_path(video)
    except utils.UnsafePathError:
        log.error("Row %s has a path escaping its directory: %r", video["id"], video["path"])
        raise HTTPException(status_code=404, detail="Video file not found.") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found.")
    return path


# --- pages -------------------------------------------------------------------


SORT_OPTIONS = ("newest", "most_viewed")

# Enough to browse a large tag vocabulary without rendering a wall of chips.
MAX_TAG_CHIPS = 40


@app.get("/")
async def index(
    request: Request,
    sort: str = Query(default="newest"),
    q: str = Query(default="", max_length=200),
    tag: Annotated[list[str] | None, Query()] = None,
    page: int = Query(default=1, ge=1),
    folder: str | None = Query(default=None),
):
    """The catalogue grid.

    ``q`` is a substring search over title, description and tags; ``tag`` may
    be repeated and narrows conjunctively. Both are query parameters rather
    than server-side state, so two people browsing at once do not fight over
    one shared filter -- the same reasoning that already applies to ``sort``.
    """
    if sort not in SORT_OPTIONS:
        sort = "newest"

    query = q.strip()
    # Deduplicated, order preserved, and capped so a hand-written URL cannot
    # turn one page render into thousands of set comparisons per row.
    selected_tags: list[str] = []
    for value in tag or []:
        cleaned = value.strip().casefold()
        if cleaned and cleaned not in selected_tags:
            selected_tags.append(cleaned)
        if len(selected_tags) >= MAX_TAG_CHIPS:
            break

    directory = _library_or_404(folder)
    results = utils.browse_videos(
        sort_by=sort, directory=directory, query=query, tags=selected_tags, page=page
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "video_files": results.videos,
            "page": results,
            "timestamp": int(datetime.datetime.now().timestamp()),
            "sibling_folders": utils.get_sibling_folders(directory),
            "current_folder": directory.name,
            "folder": directory.name,
            "current_sort": sort,
            "current_query": query,
            "selected_tags": selected_tags,
            "available_tags": utils.collect_tags(directory)[:MAX_TAG_CHIPS],
            "is_filtered": bool(query or selected_tags),
            "auth_enabled": auth.is_enabled(),
            "scan_task_id": _scan_task_for(directory),
        },
    )


@app.get("/play/{video_id}")
async def play_video(
    video_id: str,
    request: Request,
    sort: str = Query(default="newest"),
    q: str = Query(default="", max_length=200),
    tag: Annotated[list[str] | None, Query()] = None,
    folder: str | None = Query(default=None),
):
    """The player, with a back link to the current catalogue view."""
    directory = _library_or_404(folder)
    video = _get_video_or_404(video_id, directory)
    if sort not in SORT_OPTIONS:
        sort = "newest"

    query = q.strip()
    selected_tags = [value.strip().casefold() for value in (tag or []) if value.strip()]

    # Opening the player counts as a view; video range requests do not.
    database.record_view(video_id, directory)

    # The back link keeps the catalogue's sort and filters.
    context = urlencode(
        [("sort", sort), ("folder", directory.name)]
        + ([("q", query)] if query else [])
        + [("tag", value) for value in selected_tags]
    )

    return templates.TemplateResponse(
        request,
        "play_mp4.html",
        {
            "video_id": video_id,
            "folder": directory.name,
            "video_title": video.get("title", ""),
            "video_description": video.get("description", ""),
            "video_tags": video.get("tags", []),
            "has_subtitles": utils.subtitle_path(video) is not None,
            "back_url": f"/?{context}",
        },
    )


# --- authentication ----------------------------------------------------------


class LoginRequest(BaseModel):
    password: str = Field(max_length=500)
    next: str = Field(default="/", max_length=2048)


@app.get("/login")
async def login_form(request: Request, next: str = Query(default="/")):
    """The password form. Reachable without a session, by definition."""
    if not auth.is_enabled():
        return RedirectResponse(url="/", status_code=303)
    if auth.verify_token(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse(url=auth.safe_next(next), status_code=303)

    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": auth.safe_next(next), "error": ""},
        status_code=200,
    )


@app.post("/login")
async def login(request: Request, payload: LoginRequest):
    if not auth.is_enabled():
        raise HTTPException(status_code=404, detail="Authentication is not enabled.")

    address = client_ip(request) or "unknown"
    locked = auth.throttle.locked_for(address)
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {locked} seconds.",
        )

    if not auth.check_password(payload.password):
        auth.throttle.record_failure(address)
        log.warning("Failed login from %s", address)
        raise HTTPException(status_code=401, detail="Incorrect password.")

    auth.throttle.reset(address)
    response = JSONResponse({"detail": "Signed in.", "next": auth.safe_next(payload.next)})
    auth.set_session_cookie(response, auth.issue_token())
    return response


@app.post("/logout")
async def logout():
    response = JSONResponse({"detail": "Signed out."})
    auth.clear_session_cookie(response)
    return response


# --- streaming ---------------------------------------------------------------


@app.get("/videos/{video_id}")
async def stream_video(
    video_id: str,
    range: str | None = Header(default=None),
    folder: str | None = Query(default=None),
):
    """Serve a video, honouring byte ranges so clients can seek."""
    video = _get_video_or_404(video_id, _library_or_404(folder))
    path = _resolved_video_path(video)

    file_size = path.stat().st_size
    mime_type = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")

    try:
        byte_range = parse_range(range, file_size)
    except RangeNotSatisfiable:
        # RFC 9110 requires the unsatisfied-range form of Content-Range here.
        return Response(
            status_code=416,
            headers={
                "Content-Range": f"bytes */{file_size}",
                "Accept-Ranges": "bytes",
            },
        )

    if byte_range is None:
        # No usable Range header: the whole entity, as 200, not a bogus 206.
        return StreamingResponse(
            downloads.iter_file_range(path, 0, max(file_size - 1, 0), STREAM_CHUNK_SIZE),
            status_code=200,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Content-Type": mime_type,
            },
        )

    start, end = byte_range
    return StreamingResponse(
        downloads.iter_file_range(path, start, end, STREAM_CHUNK_SIZE),
        status_code=206,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": mime_type,
        },
    )


@app.get("/videos/{video_id}/subtitles")
async def video_subtitles(video_id: str, folder: str | None = Query(default=None)):
    """A sidecar subtitle track, served as WebVTT.

    SubRip is what people have on disk and WebVTT is the only thing a browser
    will accept through <track>, so the conversion happens here rather than
    asking anyone to convert their files.
    """
    video = _get_video_or_404(video_id, _library_or_404(folder))
    path = utils.subtitle_path(video)
    if path is None:
        raise HTTPException(status_code=404, detail="No subtitles for this video.")

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Could not read subtitles %s: %s", path, exc)
        raise HTTPException(status_code=404, detail="No subtitles for this video.") from None

    return Response(
        content=utils.to_webvtt(text),
        media_type="text/vtt; charset=utf-8",
    )


# --- library management ------------------------------------------------------


class ChangeDirectoryRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=255)


@app.post("/api/change-directory")
async def change_directory(payload: ChangeDirectoryRequest):
    """Switch to a sibling library.

    Only real direct children of the configured parent are accepted. Joining
    an arbitrary path would let an absolute name repoint the application.
    """
    target = _library_or_404(payload.folder)
    # This remains the saved default for old URLs and a fresh visit to `/`.
    # Requests from a page carry their own folder, so other tabs stay stable.
    database.set_current_dir(target)
    task_id = _schedule_scan(target)
    return {
        "message": f"Directory changed to {payload.folder}",
        "folder": target.name,
        "url": f"/?{urlencode({'folder': target.name})}",
        "task_id": task_id,
    }


@app.get("/api/task-status/{task_id}")
async def get_task_status(task_id: str):
    task = registry.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/api/tasks")
async def list_tasks():
    """Every job this process knows about, running or recently finished.

    A download started in one tab was previously invisible everywhere else,
    including after a reload of the tab that started it -- the task id lived
    only in that page's JavaScript. The registry always knew; nothing exposed
    it.
    """
    return {"tasks": registry.list_all()}


@app.post("/api/scan")
async def rescan_library(folder: str | None = Query(default=None)):
    """Re-scan the current folder on demand.

    Scanning only ever happened at startup and on a folder switch, so a file
    copied into the library while the server was running stayed invisible
    until it was restarted.
    """
    directory = _library_or_404(folder)
    return {"task_id": _schedule_scan(directory)}


# --- downloading -------------------------------------------------------------


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


@app.post("/api/download")
async def download_video(
    payload: DownloadRequest,
    background_tasks: BackgroundTasks,
    folder: str | None = Query(default=None),
):
    """Accept either a direct media URL or a booru post page.

    A booru post is resolved here rather than inside the background task so the
    user finds out immediately, and specifically, that the post they pasted is
    a PNG or is login-restricted -- instead of watching a progress bar and then
    reading a generic failure.
    """
    url = payload.url.strip()
    directory = _library_or_404(folder)

    # A tag search is a different job -- many posts, resolved in the worker
    # because the search itself is a network call -- but it comes in through
    # the same box and returns the same task_id shape, so the UI is unchanged.
    if boorus.find_search(url) is not None:
        task_id = registry.create("import")
        background_tasks.add_task(
            process_import_task,
            task_id,
            url,
            directory,
            settings.import_limit,
        )
        return {"task_id": task_id}

    page_url: str | None = None
    title: str | None = None
    tags: list[str] = []

    if boorus.find_site(url) is not None:
        try:
            # Resolution makes two blocking HTTP calls; the event loop is
            # serving in-flight video streams and must not do them.
            post = await run_in_threadpool(boorus.resolve_post, url)
        except boorus.BooruError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except (OSError, ValueError) as exc:
            log.warning("Booru resolution of %s failed: %s", url, exc)
            raise HTTPException(
                status_code=400, detail=f"Could not read that booru page: {exc}"
            ) from None
        page_url, title, url = post.page_url, post.title, post.file_url
        # A booru post already carries the vocabulary this catalogue wants to
        # search by, so the import brings it along rather than landing untagged.
        tags = list(post.tags)

    extension = downloads.url_extension(url)
    if extension not in downloads.ALLOWED_EXTENSIONS:
        supported = ", ".join(downloads.ALLOWED_EXTENSIONS)
        if page_url is not None:
            detail = (
                f"That post's original file is {extension or 'of an unknown type'}; "
                f"this server stores video files only ({supported})."
            )
        else:
            detail = f"URL must point at a video file ({supported})."
        raise HTTPException(status_code=400, detail=detail)

    # The cheap half of duplicate detection, and it runs before the SSRF guard
    # on purpose: both are string comparisons against rows we already hold,
    # while assert_safe_url does a DNS lookup. Checking first means a URL
    # already in the folder is answered without touching the network, and
    # reports "already here" rather than whatever the resolver had to say.
    existing = database.find_duplicate(directory, source_url=url, page_url=page_url)
    if existing is not None and not settings.allow_duplicates:
        raise HTTPException(
            status_code=409,
            detail=f"Already in this folder as {existing.get('title') or existing['id']!r}.",
        )

    try:
        downloads.assert_safe_url(url)
    except downloads.UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    task_id = registry.create("download")
    background_tasks.add_task(
        process_download_task,
        task_id,
        url,
        extension,
        directory,
        page_url=page_url,
        title=title,
        tags=tags,
    )
    return {"task_id": task_id}


class DuplicateContent(Exception):
    """The downloaded bytes are already catalogued in this directory."""

    def __init__(self, existing: dict[str, Any]):
        self.existing = existing
        super().__init__(
            "That file is already in this folder as "
            f"{existing.get('title') or existing['id']!r}."
        )


def store_remote_video(
    url: str,
    extension: str,
    directory: Path,
    *,
    page_url: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    on_stage: Callable[[str, int], None] | None = None,
) -> str:
    """Fetch, transcode if needed, thumbnail, and register one remote video.

    Returns the new video's id. Raises ``DuplicateContent`` when the bytes are
    already catalogued, and whatever the download or transcode raised
    otherwise; the caller decides how that reaches the user, which is what
    lets one download and a batch import share this code.

    ``page_url`` is the booru post a direct URL came from, when there was one.
    It is recorded as the description so the entry can be traced back, and sent
    as the Referer because booru CDNs commonly refuse hotlinked requests.
    ``tags`` are the post's own tags, when the site's API supplied them.
    """
    video_id = str(uuid.uuid4())
    remote_name = downloads.url_filename(url) or f"{video_id}{extension}"
    report = on_stage or (lambda stage, percent: None)

    report("downloading", 0)
    response = downloads.open_stream(
        url, headers={"Referer": page_url} if page_url else None
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        # The remote filename is metadata only; the file on disk is named
        # from our own UUID so a hostile name cannot steer the write.
        source = Path(tmp_dir) / f"download{extension}"

        def on_progress(fraction: float | None) -> None:
            if fraction is not None:
                report("downloading", int(fraction * 30))

        with response:
            downloads.stream_to_file(response, source, on_progress)

        # The thorough half of duplicate detection: two different URLs can
        # serve the same file, and a booru re-upload is exactly that. Checked
        # before the transcode, which is the expensive step worth skipping.
        source_hash = utils.file_digest(source)
        duplicate = database.find_duplicate(directory, source_hash=source_hash)
        if duplicate is not None and not settings.allow_duplicates:
            raise DuplicateContent(duplicate)

        mp4_name = f"{video_id}.mp4"
        destination = directory / mp4_name
        archived_original = None

        if extension == ".mp4":
            shutil.move(str(source), destination)
            report("downloading", 60)
        else:
            report("converting", 30)
            # Often a remux rather than a re-encode: plenty of remote MKVs
            # and MOVs already hold H.264 that MP4 can carry untouched.
            utils.convert_to_mp4(source, destination)
            report("converting", 60)

            archive = utils.get_originals_dir(directory)
            archive.mkdir(parents=True, exist_ok=True)
            archived_original = utils.get_unique_filename(
                f"{video_id}_original{extension}", archive
            )
            shutil.move(str(source), archive / archived_original)

        report("generating_thumbnail", 80)
        try:
            utils.generate_thumbnail(destination, utils.thumbnail_path(video_id))
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            # A missing thumbnail is cosmetic; the video is still usable.
            log.warning("Thumbnail generation failed for %s: %s", destination, exc)

        database.add_video_to_db(
            {
                "id": video_id,
                "directory": str(directory.resolve()),
                "original_filename": remote_name,
                # A booru names its files by md5, which makes a useless
                # label; the resolver supplies "gelbooru 12345" instead.
                "title": title or Path(remote_name).stem,
                "path": mp4_name,
                "thumbnail_path": f"{video_id}.jpg",
                "creation_date": datetime.datetime.now().isoformat(),
                "description": page_url or "",
                "tags": list(tags or []),
                # The URL and the bytes it served, so the same video is
                # recognisable on a later re-download either way.
                "source_url": url,
                "source_hash": source_hash,
                **utils.probe_media(destination).as_row_fields(),
                "source_name": None,
                "original_webm": archived_original,
            }
        )

    return video_id


def process_download_task(
    task_id: str,
    url: str,
    extension: str,
    directory: Path,
    page_url: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """One remote download, reported through the task registry."""
    def report(stage: str, percent: int) -> None:
        registry.update(task_id, status=stage, progress=percent)

    try:
        store_remote_video(
            url, extension, directory,
            page_url=page_url, title=title, tags=tags, on_stage=report,
        )
        registry.update(task_id, status="completed", progress=100)
    except DuplicateContent as exc:
        registry.update(task_id, status="failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - reported through the task record
        log.exception("Download task %s failed", task_id)
        registry.update(task_id, status="failed", error=str(exc))


def process_import_task(task_id: str, search_url: str, directory: Path, limit: int) -> None:
    """Import every video post matching a booru tag search.

    Posts are fetched one at a time rather than concurrently: a booru will
    rate-limit or block a client that opens a dozen parallel connections, and
    a batch that gets the server banned is worse than a slow one.

    One post failing does not abandon the rest. The task reports how many were
    added, skipped as duplicates, and failed, so a partial result is legible
    instead of looking like a total failure.
    """
    try:
        registry.update(task_id, status="searching", progress=0)
        posts = boorus.resolve_search(search_url, limit)
    except boorus.BooruError as exc:
        registry.update(task_id, status="failed", error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - reported through the task record
        log.exception("Booru search %s failed", search_url)
        registry.update(task_id, status="failed", error=str(exc))
        return

    if not posts:
        registry.update(
            task_id, status="failed", error="That search matched no video posts."
        )
        return

    added = skipped = failed = 0
    for index, post in enumerate(posts):
        # Progress is per post; the per-file stages within one download would
        # make the bar jump backwards on every item.
        base = int(index / len(posts) * 100)
        registry.update(task_id, status="importing", progress=base)

        extension = downloads.url_extension(post.file_url)
        if extension not in downloads.ALLOWED_EXTENSIONS:
            skipped += 1
            continue

        # The URL-level check, which costs nothing, before the byte-level one
        # inside store_remote_video, which costs a download.
        if not settings.allow_duplicates and database.find_duplicate(
            directory, source_url=post.file_url, page_url=post.page_url
        ):
            skipped += 1
            continue

        try:
            downloads.assert_safe_url(post.file_url)
            store_remote_video(
                post.file_url, extension, directory,
                page_url=post.page_url, title=post.title, tags=list(post.tags),
            )
            added += 1
        except DuplicateContent:
            skipped += 1
        except Exception as exc:  # noqa: BLE001 - one post must not sink the batch
            log.warning("Importing %s failed: %s", post.page_url, exc)
            failed += 1

    summary = f"Imported {added} of {len(posts)} ({skipped} already here, {failed} failed)"
    log.info("%s from %s", summary, search_url)

    if added == 0 and failed:
        registry.update(task_id, status="failed", error=summary)
        return
    registry.update(task_id, status="completed", progress=100, error=None, detail=summary)


# --- metadata ----------------------------------------------------------------


MAX_TAG_LENGTH = 100


class UpdateVideoRequest(BaseModel):
    """A *partial* update: an omitted field is left as it was.

    Every field used to be mandatory, so the rename button -- which only has a
    title to send -- posted an empty description and an empty tag list along
    with it, silently destroying both. For a video imported from a booru that
    meant losing its tags and the post URL it came from, which is the only
    record of where the file originated.
    """

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = Field(default=None, max_length=50)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, tags: list[str] | None) -> list[str] | None:
        """Trim, lowercase and de-duplicate, preserving the order given."""
        if tags is None:
            return None
        cleaned: list[str] = []
        for tag in tags:
            value = tag.strip().lower()
            if not value or value in cleaned:
                continue
            if len(value) > MAX_TAG_LENGTH:
                raise ValueError(f"Tag {value[:20]!r}... exceeds {MAX_TAG_LENGTH} characters")
            cleaned.append(value)
        return cleaned

    def changes(self) -> dict[str, Any]:
        """Only the fields the client actually sent."""
        return {
            key: value
            for key, value in (
                ("title", self.title),
                ("description", self.description),
                ("tags", self.tags),
            )
            if value is not None
        }


@app.post("/api/videos/{video_id}/update")
async def update_video_metadata(
    video_id: str,
    payload: UpdateVideoRequest,
    folder: str | None = Query(default=None),
):
    directory = _library_or_404(folder)
    _get_video_or_404(video_id, directory)

    changes = payload.changes()
    if not changes:
        raise HTTPException(
            status_code=400, detail="Provide at least one of title, description or tags."
        )

    updated = database.update_video_in_db(video_id, changes, directory)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update video metadata")
    return {"detail": "Video metadata updated successfully", "video": updated}


@app.delete("/api/videos/{video_id}")
async def delete_video(video_id: str, folder: str | None = Query(default=None)):
    """Delete a video, its thumbnail, and any archived original."""
    directory = _library_or_404(folder)
    video = _get_video_or_404(video_id, directory)

    try:
        utils.video_path(video).unlink(missing_ok=True)
    except (utils.UnsafePathError, OSError) as exc:
        log.warning("Could not remove video file for %s: %s", video_id, exc)

    try:
        utils.thumbnail_path(video_id).unlink(missing_ok=True)
    except (utils.UnsafePathError, OSError) as exc:
        log.warning("Could not remove thumbnail for %s: %s", video_id, exc)

    # The archived WebM is usually the largest file of the three; leaving it
    # behind meant deletions never actually freed space.
    archived = video.get("original_webm")
    if archived:
        try:
            utils.resolve_within(utils.get_originals_dir(directory), archived).unlink(
                missing_ok=True
            )
        except (utils.UnsafePathError, OSError) as exc:
            log.warning("Could not remove archived original for %s: %s", video_id, exc)

    if not database.delete_video_from_db(video_id, directory):
        raise HTTPException(status_code=500, detail="Failed to delete video from database")
    return {"detail": "Video deleted successfully"}


@app.post("/api/videos/{video_id}/thumbnail")
async def generate_custom_thumbnail(
    video_id: str,
    time: str = Query(default="00:00:01", description="Time in HH:MM:SS format"),
    folder: str | None = Query(default=None),
):
    """Regenerate a thumbnail from the frame at *time*."""
    video = _get_video_or_404(video_id, _library_or_404(folder))
    path = _resolved_video_path(video)

    try:
        # generate_thumbnail writes to a temp file and only replaces the
        # existing thumbnail once ffmpeg has actually produced a frame.
        utils.generate_thumbnail(path, utils.thumbnail_path(video_id), timestamp=time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (subprocess.SubprocessError, OSError) as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to generate thumbnail: {exc}"
        ) from None

    return {"detail": "Thumbnail successfully updated."}


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


# Branding assets ship with the code, so they sit beside it rather than in a
# configurable directory the way thumbnails do.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)
app.mount(
    "/thumbnails",
    StaticFiles(directory=str(settings.thumbnail_dir)),
    name="thumbnails",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        # uvicorn ships its own proxy-header middleware, enabled by default and
        # trusting 127.0.0.1, which rewrites scope["client"] from
        # X-Forwarded-For before any application middleware runs. Turning it off
        # keeps forwarded-header policy in one place: middleware.client_ip,
        # which honours the header only for configured trusted_proxies.
        proxy_headers=False,
    )
