"""Regression tests for defects found by the independent adversarial review.

Each test was written to FAIL on the code as reviewed, so a pass here means the defect is fixed.
"""
import errno
import io
import os
import sys
import threading
import time
from pathlib import Path

import pytest
import requests
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from tests.conftest import FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, write_client_secrets
from tests.fake_youtube import FakeYouTubeServer, SimulatedCrash
from tests.helpers import CHUNK, make_options, sha256_of, write_mp4
from tests.test_auth import write_new_format
from tests.test_uploader import make_uploader
from youtube_upload import auth, lib, main, paths, playlists, report, titles, upload_video, uploader
from youtube_upload.errors import OptionsError, RequestError, UploadCancelled
from youtube_upload.progress import ProgressInfo


@pytest.fixture
def server():
    return FakeYouTubeServer()


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state"


def _cancel_when_upload_completes(server):
    """Emulates pressing Cancel while the final chunk is on the wire."""
    flag = {"set": False}
    original = server._put

    def put(path, body, headers):
        response = original(path, body, headers)
        session = server.sessions.get(path.rsplit("/", 1)[-1])
        if session and session["size"] and session["committed"] >= session["size"]:
            flag["set"] = True
        return response
    server._put = put

    def callback(total, done):
        if flag["set"]:
            raise UploadCancelled("Cancelled by user")
    return flag, ProgressInfo(callback=callback, finish=lambda: None)


# ------------------------------------------------------------- D: cancel vs finished upload

@pytest.mark.parametrize("chunks", [1, 3])
def test_cancel_during_the_last_chunk_does_not_lose_the_uploaded_video(server, state, clock, tmp_path, chunks):
    video = write_mp4(tmp_path / "v.mp4", size=chunks * CHUNK - 10)
    flag, progress = _cancel_when_upload_completes(server)
    up = make_uploader(server, state, clock, "--skip-if-exists", "hash",
                       cancel=lambda: flag["set"], progress_factory=lambda path: progress)
    result = up.upload_one(str(video), 0, 1)             # must not raise UploadCancelled
    assert result.status == report.UPLOADED and result.video_id == "fakeVid0001"
    assert up.ledger.get(sha256_of(video))["video_id"] == "fakeVid0001"
    assert (tmp_path / "v.mp4.url").exists()              # post-upload steps were still done
    assert len(server.videos) == 1
    again = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    assert again.status == report.SKIPPED and len(server.videos) == 1


def test_run_reports_the_uploaded_video_id_when_cancelled_right_after(server, state, clock, tmp_path, monkeypatch):
    """The id of a video that did upload must reach stdout and the report even if Cancel was pressed."""
    from tests.test_cli import World
    world = World(tmp_path, monkeypatch)
    world.by_credentials[str(world.accounts_dir / "work.json")] = server
    video = write_mp4(tmp_path / "v.mp4", size=2 * CHUNK)
    flag, progress = _cancel_when_upload_completes(server)
    out = io.StringIO()
    with pytest.raises(UploadCancelled):
        main.main(world.args("--account", "work", str(video), str(write_mp4(tmp_path / "w.mp4", seed="2"))),
                  cancel_check=lambda: flag["set"], progress_factory=lambda p: progress,
                  log=lambda m: None, output=out)
    assert out.getvalue() == "fakeVid0001\n"             # the first video is reported, the second never started
    assert len(server.videos) == 1


# ------------------------------------------------- E: state-file failures must not fail an upload

@pytest.mark.parametrize("size", [CHUNK - 5, 3 * CHUNK + 5])
def test_unwritable_session_store_never_fails_the_upload(server, state, clock, tmp_path, monkeypatch, size):
    video = write_mp4(tmp_path / "v.mp4", size=size)
    real = paths.write_private_text

    def picky(path, text):
        if "sessions" in str(path):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, text)
    monkeypatch.setattr(paths, "write_private_text", picky)
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED and result.succeeded, result.message
    assert len(server.videos) == 1
    assert server.uploaded_sha256[result.video_id] == sha256_of(video)


# ------------------------------------------------- F: --credentials-file vs several accounts

def test_credentials_file_cannot_be_combined_with_several_accounts():
    options, _ = main.build_parser().parse_args(
        ["--accounts", "alice,bob", "--credentials-file", "alice_token.json"])
    with pytest.raises(OptionsError, match="--credentials-file.*several accounts"):
        main.validate_options(options, ["v.mp4"], warn=lambda m: None)


def test_credentials_file_with_one_account_label_is_still_fine():
    options, _ = main.build_parser().parse_args(
        ["--account", "alice", "--credentials-file", "alice_token.json"])
    assert main.validate_options(options, ["v.mp4"], warn=lambda m: None) == ["alice"]


# --------------------------------------------------------------- C: token must never leak

def test_logout_failure_message_does_not_contain_the_refresh_token(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "REVOKE_URI", "http://127.0.0.1:1/revoke")        # connection refused
    creds_file = write_new_format(tmp_path / "a.json", refresh_token="1//FAKE_REFRESH_TOKEN_VALUE")
    result = auth.logout(creds_file)
    assert result.removed and not result.revoked
    assert "FAKE_REFRESH_TOKEN_VALUE" not in result.message
    assert "FAKE_REFRESH_TOKEN_VALUE" not in "".join(map(str, result))
    assert "Could not reach Google" in result.message


def test_revoke_request_sends_the_token_in_the_body_not_the_url(tmp_path):
    creds_file = write_new_format(tmp_path / "a.json", refresh_token="1//FAKE_REFRESH_TOKEN_VALUE")
    seen = {}

    def post(url, **kwargs):
        seen.update(url=url, kwargs=kwargs)

        class R(object):
            status_code = 200
        return R()
    auth.logout(creds_file, http_post=post)
    assert "FAKE_REFRESH_TOKEN_VALUE" not in seen["url"]
    assert "params" not in seen["kwargs"] and seen["kwargs"]["data"] == {"token": "1//FAKE_REFRESH_TOKEN_VALUE"}


@pytest.mark.parametrize("text", [
    "GET https://upload.example/session/abc?upload_id=SECRETUPLOADID123&uploadType=resumable failed",
    "revoke?token=1%2F%2FSECRETTOKEN HTTP/1.1",
    "https://x/cb?code=4/SECRETCODE&state=abc",
    "Authorization: Bearer ya29.SECRETBEARER",
])
def test_log_output_redacts_credentials_found_in_urls(text, capsys):
    lib.debug(text)
    err = capsys.readouterr().err
    assert "SECRET" not in err and "[redacted]" in err


def test_debug_log_file_is_redacted_too(tmp_path):
    main._append_log_file("x ?access_token=SECRETACCESS")
    text = (paths.app_home() / "youtube_upload_debug.log").read_text(encoding="utf-8")
    assert "SECRETACCESS" not in text and "[redacted]" in text


# ----------------------------------------------- H: sign-in revoked in the middle of a run

def _refresh_error():
    return RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"})


def test_token_revoked_before_the_upload_is_an_auth_failure_that_stops_the_account(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.inject("POST", "/upload/youtube/v3/videos", exc=_refresh_error())
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.FAILED and result.exit_code == report.EXIT_AUTH and result.abort_account
    assert "no longer accepts" in result.message and "Traceback" not in result.message


def test_token_revoked_after_the_upload_keeps_the_upload_and_only_warns(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.inject("GET", "/youtube/v3/videos", exc=_refresh_error(), times=3)       # the verification call
    up = make_uploader(server, state, clock, "--skip-if-exists", "hash")
    result = up.upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED and result.video_id
    assert any("could not be verified" in w for w in result.warnings)
    assert up.ledger.get(sha256_of(video))                   # remembered, so no duplicate on re-run


def test_token_revoked_at_channel_lookup_is_exit_code_4(tmp_path, monkeypatch):
    from tests.test_cli import World
    world = World(tmp_path, monkeypatch)
    srv = world.account("work")
    srv.inject("GET", "/youtube/v3/channels", exc=_refresh_error())
    video = write_mp4(tmp_path / "v.mp4")
    with pytest.raises(Exception) as info:
        main.main(world.args("--account", "work", str(video)), log=lambda m: None, output=io.StringIO())
    assert info.value.report.exit_code() == report.EXIT_AUTH


# ----------------------------------------------- G: a 5xx on the status query keeps the session

def test_transient_5xx_on_the_status_query_does_not_throw_the_saved_session_away(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=6 * CHUNK)
    server.inject("PUT", "/session/", exc=SimulatedCrash(), after_bytes=2 * CHUNK, after_commit=True)
    with pytest.raises(SimulatedCrash):
        make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    server.inject("PUT", "/session/", status=503, times=2)           # hits the status query first
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED
    assert server.sessions_started == 1                                # same session, not a restart
    assert server.bytes_put == video.stat().st_size                    # nothing sent twice


def test_429_with_retry_after_on_the_status_query_is_honoured(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=4 * CHUNK)
    server.inject("PUT", "/session/", exc=SimulatedCrash(), after_bytes=2 * CHUNK, after_commit=True)
    with pytest.raises(SimulatedCrash):
        make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    server.inject("PUT", "/session/", status=429, headers={"retry-after": "20"})
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED and server.sessions_started == 1
    assert sum(clock.sleeps) >= 20


def test_a_server_that_never_advances_cannot_loop_forever(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=3 * CHUNK)
    original = server._put
    state_ = {"calls": 0}

    def stuck(path, body, headers):
        state_["calls"] += 1
        if state_["calls"] > 400:
            raise AssertionError("loop guard missing: still looping after 400 calls")
        from tests.fake_youtube import _response
        return _response(308, {"range": "bytes=0-%d" % (CHUNK - 1)}), b""
    server._put = stuck
    result = make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert result.status == report.FAILED and "not making progress" in result.message


# ----------------------------------------------- L: a failed/rejected video is not "already there"

def test_ledger_entry_for_a_video_that_failed_processing_is_not_trusted(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    first = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    server.videos[first.video_id]["status"]["uploadStatus"] = "failed"
    second = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    assert second.status == report.UPLOADED and second.video_id != first.video_id


def test_remote_index_ignores_rejected_videos(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    bad = server.add_existing_video("old", tags=["hash:" + sha256_of(video)])
    server.videos[bad]["status"]["uploadStatus"] = "rejected"
    result = make_uploader(server, state, clock, "--skip-if-exists", "hash").upload_one(str(video), 0, 1)
    assert result.status == report.UPLOADED


# ----------------------------------- M: playlist POSTs are not blindly repeated after a lost response

def test_lost_response_on_playlist_insert_does_not_add_the_video_twice(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.playlists["PLexisting0000000000"] = {"title": "Trips", "items": []}
    server.inject("POST", "/youtube/v3/playlistItems", exc=ConnectionResetError("lost"), after_commit=True)
    result = make_uploader(server, state, clock, "--playlist", "Trips").upload_one(str(video), 0, 1)
    assert result.succeeded
    assert server.playlists["PLexisting0000000000"]["items"] == [result.video_id]


def test_lost_response_on_playlist_creation_does_not_create_two(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    server.inject("POST", "/youtube/v3/playlists", exc=ConnectionResetError("lost"), after_commit=True)
    result = make_uploader(server, state, clock, "--playlist", "Trips").upload_one(str(video), 0, 1)
    assert result.succeeded
    assert [p["title"] for p in server.playlists.values()] == ["Trips"]
    assert list(server.playlists.values())[0]["items"] == [result.video_id]


# ------------------------------------------------------------------------ N: titles

@pytest.mark.parametrize("raw,expected", [
    ("Hello World", "Hello World"),                  # no-break space
    ("旅行　京都", "旅行 京都"),    # ideographic space
    ("Part One", "Part One"),                         # em space
    ("می‌خواهم", "می‌خواهم"),   # Persian ZWNJ kept
    ("a​b", "ab"),                                    # zero-width space is junk
    ("bad\x00\x07name", "badname"),
])
def test_title_cleaning_keeps_real_characters_and_spaces(raw, expected):
    assert titles.sanitize_title(raw) == expected


def test_title_cleaning_keeps_emoji_joiners():
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    assert titles.sanitize_title("Day " + family) == "Day " + family


def test_long_explicit_title_keeps_the_numbering_suffix(server, state, clock, tmp_path):
    a, b = write_mp4(tmp_path / "a.mp4", seed="1"), write_mp4(tmp_path / "b.mp4", seed="2")
    up = make_uploader(server, state, clock, "--title", "T" * 150)
    up.upload_one(str(a), 0, 2)
    up.upload_one(str(b), 1, 2)
    titles_sent = [server.sessions[s]["meta"]["snippet"]["title"] for s in ("s1", "s2")]
    assert titles_sent[0].endswith(" [1/2]") and titles_sent[1].endswith(" [2/2]")
    assert all(len(t) <= 100 for t in titles_sent) and len(set(titles_sent)) == 2


# ------------------------------------------- P: Windows Notepad writes a BOM

def test_description_file_with_utf8_bom(tmp_path):
    desc = tmp_path / "d.txt"
    desc.write_bytes(b"\xef\xbb\xbfMy description\nline 2")
    options, _ = main.build_parser().parse_args(["--description-file", str(desc)])
    main.validate_options(options, ["v.mp4"], warn=lambda m: None)
    assert options.description == "My description\nline 2"


# ----------------------------- descriptions and tags: what the API forbids is handled up front

def test_angle_brackets_in_the_description_are_replaced_with_a_warning():
    notes = []
    options, _ = main.build_parser().parse_args(["--description", "a <b>bold</b> claim"])
    main.validate_options(options, ["v.mp4"], warn=notes.append)
    assert "<" not in options.description and ">" not in options.description
    assert "bold" in options.description and any("description" in n for n in notes)


def test_overlong_description_is_refused_before_any_network():
    options, _ = main.build_parser().parse_args(["--description", "x" * 5001])
    with pytest.raises(OptionsError, match="5000"):
        main.validate_options(options, ["v.mp4"], warn=lambda m: None)
    options, _ = main.build_parser().parse_args(["--description", "é" * 2600])      # 5200 bytes
    with pytest.raises(OptionsError, match="5000"):
        main.validate_options(options, ["v.mp4"], warn=lambda m: None)


def test_bad_tags_are_cleaned_or_refused():
    notes = []
    options, _ = main.build_parser().parse_args(["--tags", "good, bad<tag>, , also good"])
    main.validate_options(options, ["v.mp4"], warn=notes.append)
    assert options.tags == "good, badtag, also good" and any("tag" in n for n in notes)
    options, _ = main.build_parser().parse_args(["--tags", ",".join("tag%03d" % i for i in range(120))])
    with pytest.raises(OptionsError, match="500"):
        main.validate_options(options, ["v.mp4"], warn=lambda m: None)


# ----------------------------------------------------- locks on filesystems without flock

@pytest.mark.skipif(os.name != "posix", reason="flock is POSIX")
def test_a_filesystem_without_file_locking_does_not_block_uploads_for_hours(tmp_path, monkeypatch):
    import fcntl
    from youtube_upload.ledger import ProcessLock

    def no_locking(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(fcntl, "flock", no_locking)
    lock = ProcessLock(tmp_path / "l.lock")
    assert lock.acquire() is True and lock.degraded is True
    lock.release()


# ----------------------------------------------------------- K: slow uplinks

def test_http_timeout_is_long_enough_for_a_chunk_on_a_slow_link(tmp_path, monkeypatch):
    secrets = write_client_secrets(tmp_path / "c.json")
    creds = Credentials("tok", refresh_token="r", client_id=FAKE_CLIENT_ID, client_secret=FAKE_CLIENT_SECRET,
                        token_uri=auth.DEFAULT_TOKEN_URI, scopes=list(auth.SCOPES))
    monkeypatch.setattr(auth, "get_credentials", lambda *a, **k: creds)
    youtube = auth.get_resource(str(secrets), str(tmp_path / "x.json"), interactive=False)
    inner = youtube._http.http
    assert inner.timeout >= 300                         # googleapiclient's default is 60 s per request
    assert 308 not in inner.redirect_codes              # resumable-upload "resume incomplete" must not redirect


# ------------------ the first-chunk window: dying after the server has everything, before the reply

@pytest.mark.parametrize("extra", [[], ["--skip-if-exists", "hash"]])
def test_single_chunk_file_survives_a_crash_after_the_server_committed(server, state, clock, tmp_path, extra):
    """Whole file in one PUT; the process dies after the server stored it but before it saw the
    reply. The session record must already exist so the restart finds the finished video."""
    video = write_mp4(tmp_path / "v.mp4", size=CHUNK - 100)
    server.inject("PUT", "/session/", exc=SimulatedCrash(), after_bytes=1, after_commit=True)
    with pytest.raises(SimulatedCrash):
        make_uploader(server, state, clock, *extra).upload_one(str(video), 0, 1)
    assert len(list((state / "sessions").glob("*.json"))) == 1       # saved when the URI was issued
    result = make_uploader(server, state, clock, *extra).upload_one(str(video), 0, 1)
    assert result.status in (report.UPLOADED, report.SKIPPED) and result.succeeded
    assert len(server.videos) == 1 and server.sessions_started == 1


def test_session_record_exists_before_the_first_byte_is_sent(server, state, clock, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=3 * CHUNK)
    seen = {}
    original = server._put

    def put(path, body, headers):
        seen.setdefault("records_at_first_put", len(list((state / "sessions").glob("*.json"))))
        return original(path, body, headers)
    server._put = put
    make_uploader(server, state, clock).upload_one(str(video), 0, 1)
    assert seen["records_at_first_put"] == 1
