"""The command line end to end with fake accounts: exit codes, isolation, validation (#5, #6, #9)."""
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import write_client_secrets
from tests.fake_youtube import FakeYouTubeServer
from tests.helpers import CHUNK, write_mp4
from youtube_upload import auth, lib, main, paths, report
from youtube_upload.errors import (AuthenticationError, OptionsError, UploadCancelled,
                                   UploadFailed)

REPO = Path(__file__).resolve().parents[1]


class World(object):
    """Fake accounts: label -> server (or an exception to raise when signing in)."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.accounts_dir = tmp_path / "accts"
        write_client_secrets(self.accounts_dir / "client_secrets.json")
        self.by_credentials = {}
        self.sign_ins = []
        monkeypatch.setattr(auth, "get_resource", self._get_resource)

    def account(self, label, server=None, error=None):
        self.by_credentials[str(self.accounts_dir / (label + ".json"))] = error or server or FakeYouTubeServer(
            "UC%s%s" % (label, "0" * (22 - len(label))))
        return self.by_credentials[str(self.accounts_dir / (label + ".json"))]

    def _get_resource(self, client_secrets, credentials_file, **kwargs):
        self.sign_ins.append((client_secrets, credentials_file, kwargs.get("interactive")))
        target = self.by_credentials[credentials_file]
        if isinstance(target, BaseException):
            raise target
        return target.client()

    def args(self, *extra):
        return ["--accounts-dir", str(self.accounts_dir), "--chunksize", str(CHUNK)] + list(extra)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def run(world, *argv, videos=()):
    out, err = io.StringIO(), []
    try:
        result = main.main(world.args(*argv) + [str(v) for v in videos], output=out, log=err.append)
        return result, out.getvalue(), err, None
    except Exception as exc:  # noqa: BLE001 - tests inspect it
        return None, out.getvalue(), err, exc


def exit_code(exc):
    return lib.catch_exceptions(main.EXIT_CODES, lambda: (_ for _ in ()).throw(exc))


# ----------------------------------------------------------- success and failure

def test_success_prints_only_video_ids_on_stdout_and_exits_zero(world, tmp_path):
    world.account("work")
    video = write_mp4(tmp_path / "v.mp4")
    result, out, err, exc = run(world, "--account", "work", videos=[video])
    assert exc is None and result.ok
    assert out == "fakeVid0001\n"
    assert any(line.startswith("Summary: 1 uploaded") for line in err)


def test_every_upload_failing_is_not_success(world, tmp_path):
    """#6: previously the CLI exited 0 even when every upload failed."""
    server = world.account("work")
    server.inject("POST", "/upload/youtube/v3/videos", status=400, reason="invalidTitle", times=2)
    a, b = write_mp4(tmp_path / "a.mp4", seed="1"), write_mp4(tmp_path / "b.mp4", seed="2")
    result, out, err, exc = run(world, "--account", "work", videos=[a, b])
    assert isinstance(exc, UploadFailed)
    assert out == ""
    assert exit_code(exc) == report.EXIT_REQUEST
    assert [r.status for r in exc.report.results] == [report.FAILED, report.FAILED]
    assert "invalidTitle" in "\n".join(exc.report.summary_lines())
    assert "2 of 2" in str(exc)


def test_partial_failure_exits_nonzero_but_keeps_the_good_upload(world, tmp_path):
    world.account("work")
    good = write_mp4(tmp_path / "good.mp4")
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video" * 200)
    result, out, err, exc = run(world, "--account", "work", videos=[bad, good])
    assert isinstance(exc, UploadFailed)
    assert out == "fakeVid0001\n"                                 # the good one still went up
    assert exit_code(exc) == report.EXIT_PARTIAL
    statuses = {Path(r.path).name: r.status for r in exc.report.results}
    assert statuses == {"bad.mp4": report.FAILED, "good.mp4": report.UPLOADED}


def test_quota_exhaustion_stops_the_account_and_marks_the_rest_not_attempted(world, tmp_path):
    server = world.account("work")
    server.inject("POST", "/upload/youtube/v3/videos", status=403, reason="quotaExceeded", times=99)
    videos = [write_mp4(tmp_path / ("v%d.mp4" % i), seed=str(i)) for i in range(3)]
    result, out, err, exc = run(world, "--account", "work", videos=videos)
    assert isinstance(exc, UploadFailed)
    assert [r.status for r in exc.report.results] == [report.FAILED, report.NOT_ATTEMPTED, report.NOT_ATTEMPTED]
    assert len(server.requests_to("POST", "/upload/youtube/v3/videos")) == 1   # did not hammer the API
    assert "quotaExceeded" in "\n".join(exc.report.summary_lines()) and exit_code(exc) == report.EXIT_REQUEST


def test_a_bad_file_does_not_stop_the_others(world, tmp_path):
    server = world.account("work")
    server.inject("POST", "/upload/youtube/v3/videos", status=400, reason="invalidTitle")
    a, b = write_mp4(tmp_path / "a.mp4", seed="1"), write_mp4(tmp_path / "b.mp4", seed="2")
    result, out, err, exc = run(world, "--account", "work", videos=[a, b])
    assert [r.status for r in exc.report.results] == [report.FAILED, report.UPLOADED]


# ------------------------------------------------------------ multiple accounts

def test_failed_sign_in_for_one_account_does_not_abort_the_other(world, tmp_path):
    """#9: an invalid refresh token for the first account used to abort the whole batch."""
    world.account("expired", error=AuthenticationError("Google no longer accepts the saved sign-in."))
    good = world.account("fresh")
    video = write_mp4(tmp_path / "v.mp4")
    result, out, err, exc = run(world, "--accounts", "expired,fresh", videos=[video])
    assert isinstance(exc, UploadFailed)
    assert out == "fakeVid0001\n" and len(good.videos) == 1
    assert "expired" in exc.report.account_errors
    assert exit_code(exc) == report.EXIT_PARTIAL
    by_account = {r.account: r.status for r in exc.report.results}
    assert by_account == {"expired": report.NOT_ATTEMPTED, "fresh": report.UPLOADED}


def test_all_accounts_unusable_reports_auth_exit_code(world, tmp_path):
    world.account("a", error=AuthenticationError("expired"))
    world.account("b", error=AuthenticationError("revoked"))
    video = write_mp4(tmp_path / "v.mp4")
    result, out, err, exc = run(world, "--accounts", "a,b", videos=[video])
    assert exit_code(exc) == report.EXIT_AUTH


def test_upload_failure_on_one_account_does_not_hide_or_stop_the_other(world, tmp_path):
    a = world.account("a")
    b = world.account("b")
    a.inject("POST", "/upload/youtube/v3/videos", status=403, reason="quotaExceeded")
    video = write_mp4(tmp_path / "v.mp4")
    result, out, err, exc = run(world, "--accounts", "a,b", videos=[video])
    assert isinstance(exc, UploadFailed)
    assert not a.videos and len(b.videos) == 1
    assert exit_code(exc) == report.EXIT_PARTIAL


def test_accounts_use_separate_credential_files_and_one_client_secret(world, tmp_path):
    world.account("work")
    world.account("home")
    video = write_mp4(tmp_path / "v.mp4")
    run(world, "--accounts", "work,home", videos=[video])
    creds = [c for _, c, _ in world.sign_ins]
    assert creds == [str(world.accounts_dir / "work.json"), str(world.accounts_dir / "home.json")]
    assert {s for s, _, _ in world.sign_ins} == {str(world.accounts_dir / "client_secrets.json")}


def test_same_file_to_two_accounts_is_two_distinct_uploads(world, tmp_path):
    a, b = world.account("work"), world.account("home")
    video = write_mp4(tmp_path / "v.mp4")
    result, out, err, exc = run(world, "--accounts", "work,home", "--skip-if-exists", "hash", videos=[video])
    assert exc is None and len(a.videos) == 1 and len(b.videos) == 1


@pytest.mark.parametrize("label", ["../evil", "..\\evil", "a/b", "a\\b", "", ".hidden", "trailing.", "CON",
                                   "nul.txt", "x" * 80, "colon:name", "star*", " lead"])
def test_unsafe_account_labels_are_rejected_before_touching_the_disk(world, tmp_path, label):
    video = write_mp4(tmp_path / "v.mp4")
    result, out, err, exc = run(world, "--account", label, videos=[video])
    if label == "":
        return  # empty means "no --account"; handled as the default account (no files outside)
    assert isinstance(exc, OptionsError) and "Invalid account label" in str(exc)
    assert not (tmp_path / "evil.json").exists() and not (world.accounts_dir.parent / "evil.json").exists()


def test_unicode_account_labels_are_allowed(world, tmp_path):
    world.account("Электроника")
    video = write_mp4(tmp_path / "v.mp4")
    result, out, err, exc = run(world, "--account", "Электроника", videos=[video])
    assert exc is None


# --------------------------------------------------------- up-front validation

@pytest.mark.parametrize("argv,fragment", [
    ([], "No video files"),
    (["--privacy", "secret"], "--privacy"),
    (["--publish-at", "tomorrow"], "ISO 8601"),
    (["--publish-at", "2026-13-45T00:00:00Z"], "real date"),
    (["--recording-date", "yesterday"], "ISO 8601"),
    (["--location", "latitude=95,longitude=0"], "latitude"),
    (["--location", "lat=1,lon=2"], "unknown key"),
    (["--location", "latitude=1"], "both latitude and longitude"),
    (["--location", "latitude=abc,longitude=2"], "numbers"),
    (["--thumbnail", "/no/such/file.jpg"], "Thumbnail file not found"),
    (["--chunksize", "1000"], "multiple of 262144"),
    (["--max-retries", "-1"], "negative"),
    (["--description-file", "/no/such/file.txt"], "--description-file"),
    (["--title-template", "{nope}"], "--title-template"),
    (["--embeddable", "maybe"], "--embeddable"),
    (["--category", "NotACategory"], "not a valid category"),
    (["--accounts", " , "], "no account names"),
    (["--auth-port", "70000"], "--auth-port"),
])
def test_bad_options_fail_fast_with_exit_code_2_and_no_network(world, tmp_path, argv, fragment):
    video = write_mp4(tmp_path / "v.mp4")
    args = argv if argv == [] else argv
    result, out, err, exc = run(world, *args, videos=[] if argv == [] else [video])
    assert isinstance(exc, (OptionsError, Exception))
    assert fragment in str(exc)
    assert exit_code(exc) in (2, 3)    # options (2) / category (3, historic)
    assert world.sign_ins == []         # nothing was contacted


def test_thumbnail_must_be_an_image(world, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    fake = tmp_path / "t.jpg"
    fake.write_bytes(b"this is text")
    result, out, err, exc = run(world, "--thumbnail", str(fake), videos=[video])
    assert "not a JPEG or PNG" in str(exc)
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024))
    result, out, err, exc = run(world, "--thumbnail", str(big), videos=[video])
    assert "2 MB" in str(exc)


def test_publish_at_without_zone_is_made_explicit(world, tmp_path):
    server = world.account("work")
    video = write_mp4(tmp_path / "v.mp4")
    result, out, err, exc = run(world, "--account", "work", "--publish-at", "2099-01-01T10:00:00", videos=[video])
    assert exc is None
    assert any("no time zone" in line for line in err)
    assert server.sessions["s1"]["meta"]["status"]["publishAt"] == "2099-01-01T10:00:00Z"


def test_offset_timestamps_are_accepted(world, tmp_path):
    world.account("work")
    video = write_mp4(tmp_path / "v.mp4")
    result, out, err, exc = run(world, "--account", "work", "--publish-at", "2099-01-01T10:00:00+05:30", videos=[video])
    assert exc is None


def test_description_file_is_used(world, tmp_path):
    server = world.account("work")
    desc = tmp_path / "d.txt"
    desc.write_text("from a file é", encoding="utf-8")
    video = write_mp4(tmp_path / "v.mp4")
    run(world, "--account", "work", "--description-file", str(desc), videos=[video])
    assert server.sessions["s1"]["meta"]["snippet"]["description"] == "from a file é"


def test_missing_client_secrets_explains_where_to_put_it(tmp_path, monkeypatch):
    parser = main.build_parser()
    options, args = parser.parse_args(["--accounts-dir", str(tmp_path / "empty")])
    with pytest.raises(OptionsError) as info:
        main.resolve_account(options, "work")
    assert "client_secrets.json not found" in str(info.value)
    assert str(tmp_path / "empty" / "client_secrets.json") in str(info.value)


def test_duplicate_titles_in_one_batch_are_flagged(world, tmp_path):
    world.account("work")
    a = write_mp4(tmp_path / "x" / "clip.mp4", seed="1")
    b = write_mp4(tmp_path / "y" / "clip.mp4", seed="2")
    result, out, err, exc = run(world, "--account", "work", videos=[a, b])
    assert any("same title" in line for line in err)


# ----------------------------------------------------------------- account actions

def test_list_accounts(world, capsys):
    from tests.test_auth import write_new_format
    write_new_format(world.accounts_dir / "work.json")
    (world.accounts_dir / "other.json").write_text("junk", encoding="utf-8")
    out = io.StringIO()
    main.main(["--list-accounts", "--accounts-dir", str(world.accounts_dir)], output=out)
    assert out.getvalue().splitlines() == ["other\tunreadable", "work\tsigned-in"]


def test_login_and_logout_actions_use_the_account_files(world, monkeypatch):
    calls = []
    ports = []
    monkeypatch.setattr(auth, "login", lambda secrets, creds, **kw: ports.append(kw["port"]) or
                        calls.append(("login", Path(creds).name, kw["open_browser"])))
    monkeypatch.setattr(auth, "logout", lambda creds, revoke=True: calls.append(("logout", Path(creds).name, revoke)) or auth.LogoutResult(True, True, "Signed out."))
    main.main(["--login", "--accounts", "work,home", "--no-browser", "--auth-port", "8765",
               "--accounts-dir", str(world.accounts_dir)])
    main.main(["--logout", "--account", "work", "--no-revoke", "--accounts-dir", str(world.accounts_dir)])
    assert calls == [("login", "work.json", False), ("login", "home.json", False), ("logout", "work.json", False)]
    assert ports == [8765, 8765]


def test_legacy_default_credentials_are_copied_to_the_per_user_folder(tmp_path, monkeypatch):
    from tests.test_auth import write_legacy_format
    legacy = write_legacy_format(tmp_path / "legacy.json")
    monkeypatch.setattr(main, "_legacy_default_credentials", lambda: (legacy,))
    write_client_secrets(paths.app_home() / "client_secrets.json")
    options, _ = main.build_parser().parse_args([])
    notes = []
    context = main.resolve_account(options, None, notes.append)
    assert context.credentials == paths.app_home() / "credentials.json" and context.credentials.exists()
    assert legacy.exists()                                         # never destroys the old file
    assert any("Copied the saved sign-in" in n for n in notes)
    assert "fake-legacy-refresh" not in "\n".join(notes)


def test_default_account_uses_the_per_user_folder_not_the_source_tree(tmp_path):
    write_client_secrets(paths.app_home() / "client_secrets.json")
    options, _ = main.build_parser().parse_args([])
    context = main.resolve_account(options, None, lambda m: None)
    assert paths.app_home() in context.credentials.parents
    assert REPO not in context.credentials.parents


def test_client_secrets_in_the_source_tree_still_work_but_warn(tmp_path, monkeypatch):
    pkg = Path(main.__file__).resolve().parent
    secrets = pkg / "client_secrets.json"
    assert not secrets.exists()
    write_client_secrets(secrets)
    try:
        options, _ = main.build_parser().parse_args([])
        notes = []
        context = main.resolve_account(options, None, notes.append)
        assert context.client_secrets == secrets
        assert any("inside the source tree" in n for n in notes)
    finally:
        secrets.unlink()


# --------------------------------------------------------------- process level

def test_exit_code_mapping_covers_subclasses():
    """Regression: catch_exceptions used exit_codes[exc.__class__] and raised KeyError for subclasses."""
    class MyOptionsError(OptionsError):
        pass
    assert exit_code(MyOptionsError("x")) == 2
    assert exit_code(AuthenticationError("x")) == 4
    assert exit_code(UploadCancelled("x")) == 6
    from youtube_upload.errors import QuotaExceeded, RetryExhausted
    assert exit_code(QuotaExceeded("x")) == 3 and exit_code(RetryExhausted("x")) == 3


def test_run_exits_with_the_report_code(world, tmp_path, monkeypatch):
    world.account("work")
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"nope" * 500)
    monkeypatch.setattr(sys, "argv", ["youtube-upload"] + world.args("--account", "work", str(bad)))
    with pytest.raises(SystemExit) as info:
        main.run()
    assert info.value.code == report.EXIT_OPTIONS


def test_run_exits_zero_on_success(world, tmp_path, monkeypatch):
    world.account("work")
    video = write_mp4(tmp_path / "v.mp4")
    monkeypatch.setattr(sys, "argv", ["youtube-upload"] + world.args("--account", "work", str(video)))
    with pytest.raises(SystemExit) as info:
        main.run()
    assert info.value.code == 0


def test_run_handles_ctrl_c(monkeypatch):
    monkeypatch.setattr(main, "main", lambda argv: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(SystemExit) as info:
        main.run()
    assert info.value.code == 130


def test_no_stdout_under_pythonw_does_not_crash(world, tmp_path):
    world.account("work")
    video = write_mp4(tmp_path / "v.mp4")
    result = main.main(world.args("--account", "work", str(video)), output=None, log=lambda m: None)
    assert result.ok


def test_cancel_marks_everything_cancelled_and_raises(world, tmp_path):
    world.account("work")
    videos = [write_mp4(tmp_path / ("v%d.mp4" % i), seed=str(i)) for i in range(3)]
    with pytest.raises(UploadCancelled):
        main.main(world.args("--account", "work") + [str(v) for v in videos],
                  cancel_check=lambda: True, log=lambda m: None, output=io.StringIO())


def _cli(*argv):
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable] + list(argv), capture_output=True, text=True,
                          env=env, cwd=str(REPO), timeout=60)


def test_module_entry_point_help_and_version():
    helped = _cli("-m", "youtube_upload", "--help")
    assert helped.returncode == 0 and "--skip-if-exists" in helped.stdout
    version = _cli("-m", "youtube_upload", "--version")
    assert version.returncode == 0 and "0.9.0" in version.stdout


def test_module_entry_point_uses_exit_codes_not_tracebacks():
    result = _cli("-m", "youtube_upload")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr and "No video files" in result.stderr


def test_bin_script_help():
    result = _cli(str(REPO / "bin" / "youtube-upload"), "--help")
    assert result.returncode == 0 and "Upload videos to Youtube" in result.stdout


def test_gui_script_import_does_not_pull_in_oauth2client():
    result = _cli("-c", "import sys, youtube_upload.main; "
                  "assert 'oauth2client' not in sys.modules; "
                  "assert 'progressbar2' not in sys.modules")
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------------- progress bar

def test_cli_progress_bar_really_draws_with_the_progressbar2_package():
    """#5: `import progressbar2` always failed, so no bar was ever shown."""
    from youtube_upload import progress
    assert progress.progressbar is not None
    stream = io.StringIO()
    info = progress.get_progress_info(stream=stream)
    assert info.callback is not None
    info.callback(1000, 0)
    info.callback(1000, 500)
    info.callback(1000, 1000)
    info.finish()
    text = stream.getvalue()
    assert "100%" in text and "1000" in text.replace(" ", "").replace("B", "") or "100%" in text


def test_failed_upload_does_not_show_a_fake_100_percent():
    from youtube_upload import progress
    stream = io.StringIO()
    info = progress.get_progress_info(stream=stream)
    info.callback(1000, 0)
    info.callback(1000, 400)
    info.finish()          # upload died at 40%
    assert "100%" not in stream.getvalue()


def test_missing_progressbar_module_is_reported_once(monkeypatch):
    from youtube_upload import progress
    monkeypatch.setattr(progress, "progressbar", None)
    monkeypatch.setattr(progress, "_warned_missing", False)
    warnings = []
    first = progress.get_progress_info(warn=warnings.append)
    second = progress.get_progress_info(warn=warnings.append)
    assert first.callback is None and second.callback is None
    assert len(warnings) == 1 and "pip install progressbar2" in warnings[0]


def test_no_stderr_means_no_bar_and_no_crash():
    from youtube_upload import progress
    import sys as _sys
    saved, _sys.stderr = _sys.stderr, None
    try:
        info = progress.get_progress_info()
    finally:
        _sys.stderr = saved
    assert info.callback is None
    info.finish()


def test_failure_summary_is_printed_once_not_twice(world, tmp_path, monkeypatch, capsys):
    world.account("work")
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"nope" * 500)
    monkeypatch.setattr(sys, "argv", ["youtube-upload"] + world.args("--account", "work", str(bad)))
    with pytest.raises(SystemExit):
        main.run()
    err = capsys.readouterr().err
    # the reason appears in the live "FAILED ..." line and once in the summary, not a third time
    assert err.count("not a recognised video container") == 2
    assert err.count("Summary:") == 1
    assert "1 of 1 requested upload(s) did not complete" in err


# ------------------------------------------------ containers without a resolvable home directory

@pytest.fixture
def no_home(monkeypatch, tmp_path):
    """Emulate `docker run --user 12345`: no HOME, no passwd entry, no XDG/APPDATA."""
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)

    def boom(cls=None):
        raise RuntimeError("Could not determine home directory.")
    monkeypatch.setattr(Path, "home", classmethod(boom))
    monkeypatch.setenv("YOUTUBE_UPLOAD_HOME", str(tmp_path / "cfg"))


def test_the_app_imports_and_runs_without_a_home_directory(no_home, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA")}
    env.update(PYTHONPATH=str(REPO), YOUTUBE_UPLOAD_HOME=str(tmp_path / "cfg2"))
    code = ("import pathlib\n"
            "def boom(*a, **k): raise RuntimeError('Could not determine home directory.')\n"
            "pathlib.Path.home = classmethod(boom)\n"
            "import youtube_upload.main as m\n"
            "import sys; sys.exit(m.lib.catch_exceptions(m.EXIT_CODES, m.main, ['--list-accounts']))\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                            timeout=60, cwd=str(REPO))
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr


def test_accounts_work_without_a_home_directory(no_home, tmp_path):
    options, _ = main.build_parser().parse_args(["--accounts-dir", str(tmp_path / "a")])
    write_client_secrets(tmp_path / "a" / "client_secrets.json")
    context = main.resolve_account(options, "work", lambda m: None)
    assert context.credentials == tmp_path / "a" / "work.json"
    default_dir = paths.default_accounts_dir()                  # no legacy folder to look for
    assert default_dir == tmp_path / "cfg" / "accounts"


def test_without_home_or_override_the_error_says_what_to_set(monkeypatch):
    for name in ("YOUTUBE_UPLOAD_HOME", "HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no home"))))
    with pytest.raises(OptionsError, match="YOUTUBE_UPLOAD_HOME"):
        paths.app_home()
