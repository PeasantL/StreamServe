"""A page's folder stays stable while the saved default and scans change."""

import asyncio
import json
import threading
import time

import pytest


def _row(directory, video_id="clip"):
    return {
        "id": video_id,
        "directory": str(directory.resolve()),
        "path": "clip.mp4",
        "title": directory.name,
        "creation_date": "2024-01-01T00:00:00",
    }


def test_open_page_and_player_keep_their_folder_after_another_tab_switches(client, app_env):
    import database

    http, _ = client
    first = app_env["videos"]
    second = app_env["other"]
    for directory, payload in ((first, b"first"), (second, b"second")):
        (directory / "clip.mp4").write_bytes(payload)
        database.add_video_to_db(_row(directory))

    page = http.get("/?folder=library")
    assert 'href="/play/clip?folder=library' in page.text
    assert http.post("/api/change-directory", json={"folder": "other"}).status_code == 200
    assert http.get("/?folder=library").status_code == 200
    assert http.get("/play/clip?folder=library").status_code == 200
    assert 'src="/videos/clip?folder=library"' in http.get(
        "/play/clip?folder=library"
    ).text
    assert http.get("/videos/clip?folder=library", headers={"Range": "bytes=0-2"}).content == b"fir"
    assert http.get("/videos/clip?folder=other", headers={"Range": "bytes=0-2"}).content == b"sec"
    assert http.get("/videos/clip").content == b"second"


def test_stale_page_actions_stay_in_the_original_folder(client, app_env, monkeypatch):
    import database
    import downloads

    http, main = client
    first = app_env["videos"]
    second = app_env["other"]
    for directory in (first, second):
        (directory / "clip.mp4").write_bytes(b"video")
        database.add_video_to_db(_row(directory))
    assert http.post("/api/change-directory", json={"folder": "other"}).status_code == 200

    changed = http.post(
        "/api/videos/clip/update?folder=library", json={"title": "Only first"}
    )
    assert changed.status_code == 200
    assert database.get_video_by_id("clip", first)["title"] == "Only first"
    assert database.get_video_by_id("clip", second)["title"] == "other"

    destinations = []
    monkeypatch.setattr(downloads, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(
        main, "process_download_task", lambda *args, **kwargs: destinations.append(args[3])
    )
    response = http.post(
        "/api/download?folder=library", json={"url": "https://example.com/new.mp4"}
    )
    assert response.status_code == 200
    assert destinations == [first.resolve()]

    deleted = http.delete("/api/videos/clip?folder=library")
    assert deleted.status_code == 200
    assert database.get_video_by_id("clip", first) is None
    assert database.get_video_by_id("clip", second) is not None
    assert (second / "clip.mp4").exists()


def test_scan_requests_queue_and_reuse_the_same_folder_task(client, app_env, monkeypatch):
    import utils

    http, main = client
    entered = threading.Event()
    release = threading.Event()
    scanned = []

    def slow_scan(directory):
        scanned.append(directory)
        if directory == app_env["videos"].resolve():
            entered.set()
            assert release.wait(5)
        return {}

    # The fixture's startup scan must finish before this test controls scans.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and main._scan_task_for(app_env["videos"].resolve()):
        time.sleep(0.01)
    assert main._scan_task_for(app_env["videos"].resolve()) is None
    monkeypatch.setattr(utils, "scan_library", slow_scan)
    try:
        first = http.post("/api/scan?folder=library").json()["task_id"]
        assert entered.wait(2)
        switched = http.post("/api/change-directory", json={"folder": "other"})
        assert switched.status_code == 200
        second = switched.json()["task_id"]
        assert switched.json()["url"] == "/?folder=other"
        assert main.registry.get(second)["status"] == "queued"
        assert http.post("/api/scan?folder=other").json()["task_id"] == second
    finally:
        release.set()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and main.registry.get(second)["status"] != "completed":
        time.sleep(0.01)
    assert main.registry.get(first)["status"] == "completed"
    assert main.registry.get(second)["status"] == "completed"
    assert scanned == [app_env["videos"].resolve(), app_env["other"].resolve()]


def test_switch_still_opens_folder_if_scan_thread_cannot_start(client, app_env, monkeypatch):
    import database

    _, main = client
    def fail_start(self):
        raise RuntimeError("no threads available")

    with monkeypatch.context() as scoped:
        scoped.setattr(main.threading.Thread, "start", fail_start)
        result = asyncio.run(main.change_directory(main.ChangeDirectoryRequest(folder="other")))
    assert result["url"] == "/?folder=other"
    assert database.current_dir() == app_env["other"].resolve()
    assert main.registry.get(result["task_id"])["status"] == "failed"


def test_failed_selection_write_does_not_change_the_in_memory_folder(app_env, monkeypatch):
    import database

    original = database.current_dir()
    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(database, "_write_to_disk", fail)
    with pytest.raises(OSError):
        database.set_current_dir(app_env["other"])
    assert database.current_dir() == original
    assert json.loads(database.settings.db_file.read_text())["current_dir"] == str(original)


def test_symlink_to_nested_folder_is_neither_offered_nor_selectable(client, app_env):
    import database
    import utils

    http, _ = client
    nested = app_env["other"] / "nested"
    nested.mkdir()
    (app_env["parent"] / "alias").symlink_to(nested, target_is_directory=True)
    assert "alias" not in utils.get_sibling_folders()
    assert http.post("/api/change-directory", json={"folder": "alias"}).status_code == 404
    assert http.get("/?folder=alias").status_code == 404
    assert database.current_dir() == app_env["videos"].resolve()
