"""Full upload flow against a fake YouTube HTTP server under the real client (#6-#9, #7, #8, #15)."""
import json
import os

import pytest

from tests.fake_youtube import FakeYouTubeServer, SimulatedCrash
from tests.helpers import CHUNK, make_options, sha256_of, write_mp4
from tests.conftest import mode_of
from youtube_upload import report, uploader
from youtube_upload.errors import UploadCancelled


@pytest.fixture
def server():
    return FakeYouTubeServer()


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state"


def make_uploader(server, state, clock, *argv, label="work", cancel=None, confirm=None, **kw):
    options = make_options(*argv)
    return uploader.AccountUploader(
        server.client(), options, label, state, cancel_check=cancel, log=lambda m: None,
        sleep=clock.sleep, monotonic=clock.monotonic,
        confirm_create_playlist=confirm or (lambda title: True), **kw)


# ------------------------------------------------------------------ happy path

def test_upload_succeeds_and_is_verified_recorded_and_shortcut_written(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "videos" / "My_Holiday-2026.mp4")
    up = make_uploader(server, state, clock, "--tags", "a, b,,", "--privacy", "unlisted")
    result = up.upload_one(str(video), 0, 1)

    assert result.status == report.UPLOADED and result.succeeded
    assert result.video_id == "fakeVid0001"
    assert result.url == "https://www.youtube.com/watch?v=fakeVid0001"
    # exactly one resumable session, the whole file sent once
    assert server.sessions_started == 1
    assert server.bytes_put == video.stat().st_size
    assert server.uploaded_sha256["fakeVid0001"] == sha256_of(video)   # the exact file arrived
    # request body: derived title keeps the hyphen; no nulls; no empty tag
    meta = server.sessions["s1"]["meta"]
    assert meta["snippet"]["title"] == "My Holiday-2026"
    assert meta["snippet"]["tags"] == ["a", "b"]
    assert meta["status"]["privacyStatus"] == "unlisted"
    assert "recordingDetails" not in meta
    assert "null" not in json.dumps(meta)   # unset options are omitted, never sent as null
    # verified with videos.list, recorded locally, shortcut next to the video
    assert server.requests_to("GET", "/youtube/v3/videos")
    assert up.ledger.get(sha256_of(video))["video_id"] == "fakeVid0001"
    assert (video.parent / (video.name + ".url")).read_text().startswith("[InternetShortcut]")
    assert not (video.parent / (video.name + ".m3u")).exists()   # #11
    assert server.search_calls == 0                                 # #8: never the 100-unit fuzzy search


def test_upload_progress_reaches_total(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    seen = []

    class Prog:
        callback = staticmethod(lambda total, done: seen.append((total, done)))
        finish = staticmethod(lambda: seen.append("finished"))
    options = make_options()
    up = uploader.AccountUploader(server.client(), options, "a", state, log=lambda m: None,
                                  progress_factory=lambda path: Prog, sleep=clock.sleep,
                                  monotonic=clock.monotonic)
    up.upload_one(str(video), 0, 1)
    size = video.stat().st_size
    assert seen[-1] == "finished" and seen[-2] == (size, size)
    assert [d for t, d in seen[:-2]] == sorted(d for t, d in seen[:-2])


def test_metadata_options_reach_the_api(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    up = make_uploader(server, state, clock, "--category", "Music", "--description", "hello",
                       "--location", "latitude=10.5,longitude=-20", "--recording-date", "2025-01-02T03:04:05Z",
                       "--default-language", "en", "--embeddable", "False")
    up.upload_one(str(video), 0, 1)
    meta = server.sessions["s1"]["meta"]
    assert meta["snippet"]["categoryId"] == "10"
    assert meta["snippet"]["description"] == "hello"
    assert meta["snippet"]["defaultLanguage"] == "en"
    assert meta["status"]["embeddable"] is False
    assert meta["recordingDetails"]["location"] == {"latitude": 10.5, "longitude": -20.0}  # numbers, not strings
    assert meta["recordingDetails"]["recordingDate"] == "2025-01-02T03:04:05Z"


def test_scheduled_video_is_private_until_publish_time(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    up = make_uploader(server, state, clock, "--publish-at", "2099-01-01T00:00:00Z", "--privacy", "public")
    up.upload_one(str(video), 0, 1)
    status = server.sessions["s1"]["meta"]["status"]
    assert status["privacyStatus"] == "private" and status["publishAt"] == "2099-01-01T00:00:00Z"


# ----------------------------------------------------------------------- titles

def test_explicit_title_with_several_videos_uses_template_but_derived_titles_do_not(server, state, clock, tmp_path):
    a, b = write_mp4(tmp_path / "one.mp4", seed="1"), write_mp4(tmp_path / "two_part.mp4", seed="2")
    up = make_uploader(server, state, clock, "--title", "Trip")
    up.upload_one(str(a), 0, 2)
    up.upload_one(str(b), 1, 2)
    assert [server.sessions[s]["meta"]["snippet"]["title"] for s in ("s1", "s2")] == ["Trip [1/2]", "Trip [2/2]"]

    server2 = FakeYouTubeServer()
    up2 = make_uploader(server2, state / "other", clock)
    up2.upload_one(str(a), 0, 2)
    up2.upload_one(str(b), 1, 2)
    assert [server2.sessions[s]["meta"]["snippet"]["title"] for s in ("s1", "s2")] == ["one", "two part"]


def test_title_rewrites_are_reported_as_warnings(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "bad<name>.mp4")
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert server.sessions["s1"]["meta"]["snippet"]["title"] == "bad name"
    assert any("characters were removed" in w for w in result.warnings)


# ------------------------------------------------------------------ duplicates

def test_skip_if_exists_hash_uses_ledger_and_does_not_upload_twice(server, state, clock, tmp_path):
    # YouTube dropped the tag (or its listing lags behind): only the local record can know.
    server.drop_tags = True
    video = write_mp4(tmp_path / "v.mp4")
    first = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    second = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    assert first.status == report.UPLOADED
    assert second.status == report.SKIPPED and second.video_id == first.video_id and second.succeeded
    assert server.sessions_started == 1 and len(server.videos) == 1
    assert server.search_calls == 0


def test_ledger_entry_for_a_deleted_video_does_not_block_a_new_upload(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    first = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    del server.videos[first.video_id]
    server.upload_order.remove(first.video_id)
    second = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    assert second.status == report.UPLOADED and second.video_id != first.video_id


def test_ledger_entry_from_another_channel_is_not_trusted(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    first = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    # the recorded id now points at a video that is not on this channel (and not in its uploads)
    server.videos[first.video_id]["snippet"]["channelId"] = "UCsomeoneelse000000000"
    server.upload_order.remove(first.video_id)
    second = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    assert second.status == report.UPLOADED


def test_remote_hash_tag_from_before_the_ledger_existed_is_found_without_search(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    legacy_id = server.add_existing_video("Old upload", tags=["x", "hash:" + sha256_of(video)])
    result = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    assert result.status == report.SKIPPED and result.video_id == legacy_id
    assert server.sessions_started == 0 and server.search_calls == 0


def test_skip_if_exists_title_is_case_insensitive_and_reads_every_page(server, state, clock, tmp_path):
    server.page_size = 2
    for i in range(5):
        server.add_existing_video("Filler %d" % i)
    wanted = server.add_existing_video("My Movie")   # newest first -> page 1... then more fillers older
    for i in range(5, 9):
        server.add_existing_video("Older %d" % i)
    server.upload_order.remove(wanted)
    server.upload_order.append(wanted)               # force it onto the last page
    video = write_mp4(tmp_path / "my movie.mp4")
    result = make_uploader(server, state, clock, "--skip-if-exists", "title").upload_one(str(video), 0, 1)
    assert result.status == report.SKIPPED and result.video_id == wanted
    assert len(server.requests_to("GET", "/playlistItems")) >= 5   # paginated, not "first page only"


def test_duplicate_without_skip_option_warns_but_uploads(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    again = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert again.status == report.UPLOADED
    assert any("already uploaded" in w for w in again.warnings)


def test_same_file_to_two_channels_is_two_uploads(state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    a, b = FakeYouTubeServer("UCchannelAAAAAAAAAAAAAAA"), FakeYouTubeServer("UCchannelBBBBBBBBBBBBBBB")
    ra = make_uploader(a, state, clock, "--skip-if-exists", "hash", label="a").upload_one(str(video), 0, 1)
    rb = make_uploader(b, state, clock, "--skip-if-exists", "hash", label="b").upload_one(str(video), 0, 1)
    assert ra.status == rb.status == report.UPLOADED
    assert a.sessions_started == b.sessions_started == 1


# ------------------------------------------------------- retries and recovery

def test_transient_503_on_a_chunk_is_retried_without_duplicating_the_video(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.inject("PUT", "/session/", status=503, times=2)
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED
    assert len(server.videos) == 1 and server.sessions_started == 1
    assert server.bytes_put == video.stat().st_size            # nothing sent twice
    assert sum(clock.sleeps) > 0 and max(clock.sleeps) <= 60.5  # waited, but bounded


def test_429_retry_after_is_honoured(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.inject("PUT", "/session/", status=429, headers={"retry-after": "30"})
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED
    assert sum(clock.sleeps) >= 30


def test_connection_reset_mid_upload_resumes_from_what_the_server_has(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.inject("PUT", "/session/", exc=ConnectionResetError("reset by peer"), after_bytes=CHUNK)
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED
    assert server.sessions_started == 1 and len(server.videos) == 1
    # bytes were not re-sent from zero: total on the wire is the file size (the failed
    # chunk never reached the server in this scenario) -- never more.
    assert server.bytes_put == video.stat().st_size


def test_persistent_outage_gives_up_with_exhausted_retries_and_aborts_the_account(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.inject("PUT", "/session/", status=503, times=10 ** 6)
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.FAILED and result.abort_account
    assert "failing" in result.message and "503" in result.message
    assert result.exit_code == report.EXIT_REQUEST
    assert len(server.videos) == 0


@pytest.mark.parametrize("status,reason,exit_code,abort,fragment", [
    (403, "quotaExceeded", report.EXIT_REQUEST, True, "quotaExceeded"),
    (400, "uploadLimitExceeded", report.EXIT_REQUEST, True, "uploadLimitExceeded"),
    (401, None, report.EXIT_AUTH, True, "401"),
    (403, "forbidden", report.EXIT_AUTH, True, "forbidden"),
    (403, "youtubeSignupRequired", report.EXIT_AUTH, True, "youtubeSignupRequired"),
    (400, "invalidTitle", report.EXIT_REQUEST, False, "invalidTitle"),
])
def test_fatal_api_errors_are_not_retried_and_are_classified(server, state, clock, tmp_path,
                                                             status, reason, exit_code, abort, fragment):
    video = write_mp4(tmp_path / "v.mp4")
    server.inject("POST", "/upload/youtube/v3/videos", status=status, reason=reason)
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.FAILED and not result.succeeded
    assert result.exit_code == exit_code and result.abort_account is abort
    assert fragment in result.message
    assert len(server.requests_to("POST", "/upload/youtube/v3/videos")) == 1  # no retry
    assert clock.sleeps == []


def test_a_response_without_a_video_id_is_not_success(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.omit_id_in_final_response = True
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.FAILED
    assert "valid video id" in result.message and "Studio" in result.message
    assert not (video.parent / (video.name + ".url")).exists()


def test_youtube_rejecting_the_video_is_a_failure_and_is_not_remembered(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.final_upload_status = "rejected"
    up = make_uploader(server, state, clock, "--skip-if-exists", "hash")
    result = up.upload_one(str(video), 0, 1)
    assert result.status == report.FAILED and "rejected" in result.message
    assert up.ledger.get(sha256_of(video)) is None


def test_verification_failure_due_to_network_only_warns(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.inject("GET", "/youtube/v3/videos", status=500, times=10 ** 6)
    # the existing-video check isn't used (no skip option); verification keeps failing
    up = make_uploader(server, state, clock)
    result = up.upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED and result.video_id
    assert any("could not be verified" in w for w in result.warnings)


def test_no_verify_option_skips_the_verification_call(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    make_uploader(server, state, clock, "--no-verify").upload_one(str(video), 0, 1)
    assert server.requests_to("GET", "/youtube/v3/videos") == []


def test_missing_hash_tag_on_youtube_is_reported_not_fatal(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.drop_tags = True
    result = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED
    assert any("duplicate-detection tag" in w for w in result.warnings)


# ------------------------------------------------- resume after the process dies

def test_killed_process_resumes_the_same_upload_and_never_duplicates(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "big.mp4", size=6 * CHUNK + 5)
    # the process dies right after the server committed 2 chunks (response lost)
    server.inject("PUT", "/session/", exc=SimulatedCrash(), after_bytes=2 * CHUNK, after_commit=True)
    first = make_uploader(server, state, clock)
    with pytest.raises(SimulatedCrash):
        first.upload_one(str(video), 0, 1)
    assert server.sessions_started == 1 and not server.videos
    session_files = list((state / "sessions").glob("*.json"))
    assert len(session_files) == 1
    if os.name == "posix":
        assert mode_of(session_files[0]) == 0o600           # the URI is a capability
    sent_before = server.bytes_put

    # a brand-new process (new objects, same state dir) picks the session up
    second = make_uploader(server, state, clock)
    result = second.upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED
    assert server.sessions_started == 1 and len(server.videos) == 1      # same session, one video
    assert sent_before == 2 * CHUNK
    assert server.bytes_put - sent_before == video.stat().st_size - 2 * CHUNK   # only the remainder
    assert server.uploaded_sha256[result.video_id] == sha256_of(video)         # resumed upload is byte-exact
    assert list((state / "sessions").glob("*.json")) == []               # cleaned up on success


def test_resume_where_the_server_already_has_the_whole_file_uses_its_result(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    size = video.stat().st_size
    # all bytes arrive, then the process dies before reading the response
    server.inject("PUT", "/session/", exc=SimulatedCrash(), after_bytes=size, after_commit=True)
    with pytest.raises(SimulatedCrash):
        make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED
    assert len(server.videos) == 1 and server.sessions_started == 1


def test_changed_metadata_does_not_resume_the_old_session(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=4 * CHUNK)
    server.inject("PUT", "/session/", exc=SimulatedCrash(), after_bytes=2 * CHUNK, after_commit=True)
    with pytest.raises(SimulatedCrash):
        make_uploader(server, state, clock, "--title", "Old title").upload_one(str(video), 0, 1)
    assert len(list((state / "sessions").glob("*.json"))) == 1   # there was something to (not) resume
    result = make_uploader(server, state, clock, "--title", "New title").upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED
    assert server.sessions_started == 2
    assert server.videos[result.video_id]["snippet"]["title"] == "New title"


def test_expired_session_on_the_server_falls_back_to_a_fresh_upload(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=4 * CHUNK)
    server.inject("PUT", "/session/", exc=SimulatedCrash(), after_bytes=2 * CHUNK, after_commit=True)
    with pytest.raises(SimulatedCrash):
        make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert len(list((state / "sessions").glob("*.json"))) == 1
    server.sessions.clear()    # Google forgot the session
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED and server.sessions_started == 2


def test_stale_session_record_is_ignored(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=4 * CHUNK)
    server.inject("PUT", "/session/", exc=SimulatedCrash(), after_bytes=2 * CHUNK, after_commit=True)
    with pytest.raises(SimulatedCrash):
        make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    (record,) = (state / "sessions").glob("*.json")
    data = json.loads(record.read_text())
    data["created"] -= 30 * 24 * 3600
    record.write_text(json.dumps(data))
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED and server.sessions_started == 2


def test_corrupt_session_record_is_quarantined_not_fatal(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=4 * CHUNK)
    server.inject("PUT", "/session/", exc=SimulatedCrash(), after_bytes=2 * CHUNK, after_commit=True)
    with pytest.raises(SimulatedCrash):
        make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    (record,) = (state / "sessions").glob("*.json")
    record.write_text("{ truncated")
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED
    assert list((state / "sessions").glob("*.corrupt-*"))


def test_cancelled_upload_keeps_its_session_for_later(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=5 * CHUNK)
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 6      # after a couple of chunks
    up = make_uploader(server, state, clock, cancel=cancel)
    with pytest.raises(UploadCancelled):
        up.upload_one(str(video), 0, 1)
    assert not server.videos and len(list((state / "sessions").glob("*.json"))) == 1
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED and server.sessions_started == 1


# ------------------------------------------------------------------ invalid input

def test_invalid_files_fail_that_file_with_a_reason(server, state, clock, tmp_path):
    up = make_uploader(server, state, clock)
    cases = {
        "notes.txt": b"x" * 5000,
        "empty.mp4": b"",
        "zeros.mp4": b"\x00" * 5000,
        "text_in_mp4.mp4": b"hello world this is not video\n" * 200,
    }
    for name, content in cases.items():
        p = tmp_path / name
        p.write_bytes(content)
        result = up.upload_one(str(p), 0, 1)
        assert result.status == report.FAILED and result.exit_code == report.EXIT_OPTIONS, name
        assert not result.abort_account
    missing = up.upload_one(str(tmp_path / "gone.mp4"), 0, 1)
    assert missing.status == report.FAILED and "does not exist" in missing.message
    assert server.sessions_started == 0


def test_bad_category_fails_before_any_upload(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    up = make_uploader(server, state, clock)
    up.options.category = "NotACategory"
    result = up.upload_one(str(video), 0, 1)
    assert result.status == report.FAILED and "not a valid category" in result.message
    assert server.sessions_started == 0


# --------------------------------------------------------------------- follow-ups

def test_playlist_is_created_once_and_reused_for_the_batch(server, state, clock, tmp_path):
    a, b = write_mp4(tmp_path / "a.mp4", seed="1"), write_mp4(tmp_path / "b.mp4", seed="2")
    asked = []
    up = make_uploader(server, state, clock, "--playlist", "Trips",
                       confirm=lambda title: asked.append(title) or True)
    ra, rb = up.upload_one(str(a), 0, 2), up.upload_one(str(b), 1, 2)
    assert ra.succeeded and rb.succeeded
    assert asked == ["Trips"]
    ((pid, playlist),) = server.playlists.items()
    assert playlist["items"] == [ra.video_id, rb.video_id]
    assert len(server.requests_to("GET", "/youtube/v3/playlists")) == 1   # looked up once


def test_declined_playlist_creation_is_a_reported_problem_not_silence(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    result = make_uploader(server, state, clock, "--playlist", "Nope",
                           confirm=lambda t: False).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED and not result.succeeded
    assert any("Nope" in p for p in result.problems)


def test_thumbnail_failure_is_reported_and_the_upload_is_kept(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    thumb = tmp_path / "t.png"
    thumb.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    server.inject("POST", "/thumbnails/set", status=403, reason="forbidden")
    result = make_uploader(server, state, clock, "--thumbnail", str(thumb)).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED and result.video_id
    assert not result.succeeded and any("thumbnail" in p for p in result.problems)


def test_unwritable_shortcut_directory_is_a_warning(server, state, clock, tmp_path, monkeypatch):
    video = write_mp4(tmp_path / "v.mp4")
    monkeypatch.setattr(uploader, "create_internet_shortcut",
                        lambda *a: (_ for _ in ()).throw(PermissionError("read-only folder")))
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED and result.succeeded
    assert any(".url" in w and "read-only" in w for w in result.warnings)


def test_channel_without_a_youtube_channel_is_a_clear_error(state, clock, tmp_path):
    from youtube_upload.errors import AuthenticationError
    server = FakeYouTubeServer()
    server.inject("GET", "/youtube/v3/channels", status=200)  # placeholder, replaced below
    server.faults.clear()
    server._route_orig = server._route

    def empty_channels(method, path, query, body, headers):
        if path.endswith("/youtube/v3/channels"):
            from tests.fake_youtube import json_response
            return json_response(200, {"items": []})
        return server._route_orig(method, path, query, body, headers)
    server._route = empty_channels
    with pytest.raises(AuthenticationError, match="no YouTube channel"):
        make_uploader(server, state, clock)
