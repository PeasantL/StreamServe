"""Player navigation, sidecar subtitles and the shortcut wiring."""

import pytest

import utils


@pytest.fixture
def library(app_env):
    """Three videos, newest first: c, b, a."""
    import database

    for index, (video_id, title) in enumerate([("a", "Alpha"), ("b", "Bravo"), ("c", "Charlie")]):
        (app_env["videos"] / f"{video_id}.mp4").write_bytes(b"data")
        database.add_video_to_db({
            "id": video_id,
            "directory": str(app_env["videos"].resolve()),
            "path": f"{video_id}.mp4",
            "title": title,
            "tags": ["odd"] if index % 2 == 0 else ["even"],
            "creation_date": f"2024-01-0{index + 1}T00:00:00",
            "video_codec": "h264",
        })
    return app_env


# --- neighbours ------------------------------------------------------------------


def test_the_middle_video_has_both_neighbours(library):
    """Newest first, so the order is c, b, a."""
    assert utils.find_neighbours("b") == ("c", "a")


def test_the_first_video_has_no_previous(library):
    assert utils.find_neighbours("c") == (None, "b")


def test_the_last_video_has_no_next(library):
    assert utils.find_neighbours("a") == ("b", None)


def test_neighbours_follow_the_sort_order(library):
    """Most-viewed order is a, b, c -- the reverse of the default."""
    import database

    for video_id, views in (("a", 3), ("b", 2), ("c", 1)):
        for _ in range(views):
            database.record_view(video_id)

    assert utils.find_neighbours("b", sort_by="most_viewed") == ("a", "c")


def test_neighbours_respect_an_active_tag_filter(library):
    """Next means the next video the user can see, not the next database row."""
    assert utils.find_neighbours("c", tags=["odd"]) == (None, "a")


def test_neighbours_respect_an_active_search(library):
    assert utils.find_neighbours("b", query="bravo") == (None, None)


def test_a_video_outside_the_current_view_has_no_neighbours(library):
    """Better than guessing at a position it does not occupy."""
    assert utils.find_neighbours("a", tags=["even"]) == (None, None)


def test_an_unknown_id_has_no_neighbours(library):
    assert utils.find_neighbours("missing") == (None, None)


def test_a_lone_video_has_no_neighbours(app_env):
    import database

    (app_env["videos"] / "only.mp4").write_bytes(b"data")
    database.add_video_to_db({
        "id": "only",
        "directory": str(app_env["videos"].resolve()),
        "path": "only.mp4",
        "title": "Only",
        "creation_date": "2024-01-01T00:00:00",
    })

    assert utils.find_neighbours("only") == (None, None)


# --- the player page ----------------------------------------------------------------


def test_player_shows_back_without_neighbour_controls(client, library):
    test_client, _ = client

    response = test_client.get("/play/b")

    assert response.status_code == 200
    assert 'id="back-button"' in response.text
    assert 'id="prev-button"' not in response.text
    assert 'id="next-button"' not in response.text


def test_back_link_carries_the_filter(client, library):
    test_client, _ = client

    response = test_client.get("/play/c?tag=odd&sort=newest")

    assert 'data-back-url="/?sort=newest&amp;tag=odd"' in response.text


def test_the_player_falls_back_to_the_default_sort(client, library):
    test_client, _ = client

    assert test_client.get("/play/b?sort=nonsense").status_code == 200


def test_a_missing_video_is_404(client, library):
    test_client, _ = client

    assert test_client.get("/play/nope").status_code == 404


# --- subtitles -------------------------------------------------------------------------


SRT = "1\n00:00:01,000 --> 00:00:04,000\nHello, world\n\n2\n00:00:05,500 --> 00:00:06,000\nBye\n"


def _row(app_env, video_id="a"):
    return {
        "id": video_id,
        "directory": str(app_env["videos"].resolve()),
        "path": f"{video_id}.mp4",
    }


def test_an_srt_sidecar_is_found(library, app_env):
    (app_env["videos"] / "a.srt").write_text(SRT)

    assert utils.subtitle_path(_row(app_env)).name == "a.srt"


def test_a_vtt_sidecar_is_found(library, app_env):
    (app_env["videos"] / "a.vtt").write_text("WEBVTT\n")

    assert utils.subtitle_path(_row(app_env)).name == "a.vtt"


def test_vtt_wins_when_both_exist(library, app_env):
    """It needs no conversion, so what it holds is what the browser sees."""
    (app_env["videos"] / "a.srt").write_text(SRT)
    (app_env["videos"] / "a.vtt").write_text("WEBVTT\n")

    assert utils.subtitle_path(_row(app_env)).name == "a.vtt"


def test_no_sidecar_means_no_subtitles(library, app_env):
    assert utils.subtitle_path(_row(app_env)) is None


def test_srt_becomes_valid_webvtt():
    result = utils.to_webvtt(SRT)

    assert result.startswith("WEBVTT\n\n")
    assert "00:00:01.000 --> 00:00:04.000" in result
    assert "00:00:05.500 --> 00:00:06.000" in result
    assert ",000 -->" not in result


def test_cue_text_is_left_exactly_as_it_was():
    """Rewriting it would lose formatting for no benefit."""
    result = utils.to_webvtt(SRT)

    assert "Hello, world" in result


def test_existing_webvtt_passes_through_unchanged():
    original = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n"

    assert utils.to_webvtt(original) == original


def test_a_byte_order_mark_is_stripped():
    """A BOM before WEBVTT makes browsers reject the whole track."""
    result = utils.to_webvtt("﻿WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n")

    assert result.startswith("WEBVTT")


def test_the_subtitle_endpoint_serves_vtt(client, library, app_env):
    test_client, _ = client
    (app_env["videos"] / "a.srt").write_text(SRT)

    response = test_client.get("/videos/a/subtitles")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/vtt")
    assert response.text.startswith("WEBVTT")


def test_the_subtitle_endpoint_is_404_without_a_sidecar(client, library):
    test_client, _ = client

    assert test_client.get("/videos/a/subtitles").status_code == 404


def test_the_player_declares_a_track_only_when_there_is_one(client, library, app_env):
    test_client, _ = client

    assert "<track" not in test_client.get("/play/a").text

    (app_env["videos"] / "a.srt").write_text(SRT)
    assert "<track" in test_client.get("/play/a").text


def test_subtitles_for_an_unknown_video_are_404(client, library):
    test_client, _ = client

    assert test_client.get("/videos/nope/subtitles").status_code == 404
