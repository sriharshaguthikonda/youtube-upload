"""The real Tk GUI, driven headlessly (Xvfb on Linux, the desktop session on Windows CI).

Skipped automatically where tkinter or a display is unavailable. The upload engine
runs against the fake YouTube server; nothing touches the network or a real account.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import ttk  # noqa: E402

from tests.conftest import write_client_secrets  # noqa: E402
from tests.fake_youtube import FakeYouTubeServer  # noqa: E402
from tests.helpers import CHUNK, GUI_CHUNK, write_mp4  # noqa: E402
from youtube_upload import auth, gui_app, paths, report  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def new_tk_root():
    """Create a Tk root, retrying: on the Windows CI runner one creation per run failed with
    "Can't find a usable init.tcl" although the file exists and the other 40 succeeded."""
    import gc
    last = None
    for _ in range(4):
        try:
            return tk.Tk()
        except tk.TclError as exc:
            last = exc
            gc.collect()           # let a just-destroyed interpreter finish tearing down
            time.sleep(0.3)
    pytest.skip("no display / Tk unavailable: %s" % last)


@pytest.fixture
def root():
    window = new_tk_root()
    window.withdraw()
    yield window
    try:
        for job in window.tk.call("after", "info"):      # timers outlive destroy() and would
            window.after_cancel(job)                      # fire inside the next test's window
        window.destroy()
    except tk.TclError:
        pass


class Dialogs(object):
    def __init__(self):
        self.info, self.error, self.asked = [], [], []
        self.answer = True


@pytest.fixture
def gui(root, monkeypatch):
    app = gui_app.UploadGUI(root)
    dialogs = Dialogs()
    app._info = lambda title, message: dialogs.info.append((title, message))
    app._error = lambda title, message: dialogs.error.append((title, message))
    app._confirm = lambda title, message: dialogs.asked.append((title, message)) or dialogs.answer
    app.dialogs = dialogs
    return app


def pump(app, until=lambda: True, timeout=15.0):
    """Run the Tk event loop (and so the UI queue) until ``until()`` holds."""
    end = time.time() + timeout
    while time.time() < end:
        app.root.update()
        if until():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for the GUI; log=%r errors=%r" % (
        app.log_text.get("1.0", "end")[-600:], app.errors_text.get("1.0", "end")[-600:]))


def idle(app):
    return not app._uploading and app._ui_queue.empty()


def disabled(button):
    return button.instate(["disabled"])


def text_of(widget):
    return widget.get("1.0", "end")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """One signed-in fake account whose folder is the GUI's accounts dir."""
    accounts_dir = tmp_path / "work"
    write_client_secrets(accounts_dir / "client_secrets.json")
    server = FakeYouTubeServer()
    credentials = str(accounts_dir / "work.json")
    calls = []

    def get_resource(client_secrets, credentials_file, **kwargs):
        calls.append((client_secrets, credentials_file, kwargs))
        if credentials_file != credentials:
            raise AssertionError("unexpected credentials file %s" % credentials_file)
        return server.client()
    monkeypatch.setattr(auth, "get_resource", get_resource)

    class W(object):
        pass
    w = W()
    w.dir, w.server, w.credentials, w.calls = accounts_dir, server, credentials, calls
    return w


def select_account(app, world):
    app.accounts_dir_var.set(str(world.dir))
    app._commit_accounts_dir()          # what pressing Enter / leaving the field does
    pump(app, lambda: app.account_var.get() == "work")


def start_upload(app, world, videos, **fields):
    select_account(app, world)
    for name, value in fields.items():
        getattr(app, name).set(value)
    app._set_video_selection([str(v) for v in videos])
    app._upload()


# ------------------------------------------------------------------ construction

def test_window_builds_with_all_account_and_upload_controls(gui):
    for attr in ("title_var", "tags_var", "category_var", "playlist_var", "privacy_var", "publish_at_var",
                 "skip_if_exists_var", "secrets_var", "credentials_var", "account_var", "accounts_var",
                 "accounts_dir_var", "open_browser_var", "open_link_var", "debug_var",
                 "always_on_top_var", "theme_var", "video_path_var"):
        assert hasattr(gui, attr), attr
    for button in (gui.upload_button, gui.cancel_button, gui.sign_in_button, gui.sign_out_button):
        assert isinstance(button, ttk.Button)
    assert disabled(gui.cancel_button) and not disabled(gui.upload_button)
    assert gui.skip_if_exists_var.get() == "hash" and gui.open_browser_var.get() is True


def test_window_builds_without_stderr_like_pythonw(root, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdout", None)
    app = gui_app.UploadGUI(root)
    app._log("hello")
    app._log_error("oops")
    root.update()
    assert "hello" in text_of(app.log_text) and "oops" in text_of(app.errors_text)


def test_errors_pane_uses_the_danger_colour_and_follows_the_theme(gui):
    assert str(gui.errors_text.cget("foreground")) == gui.palette["danger"]
    gui.theme_var.set("light")
    gui.root.update()
    assert str(gui.errors_text.cget("foreground")) == gui.palette["danger"]
    assert str(gui.errors_text.cget("background")) == gui.palette["surface"]


def test_many_files_grow_the_scroll_area_instead_of_squeezing_the_rows(gui, tmp_path):
    """Regression: with a pinned content height, extra progress rows were squashed to slivers."""
    gui.root.geometry("800x700")
    gui.root.deiconify()
    gui.root.update()
    files = [str(write_mp4(tmp_path / "many" / ("f%02d.mp4" % i), size=2048, seed=str(i))) for i in range(25)]
    gui._set_video_selection(files)
    pump(gui, lambda: int(gui._canvas.itemcget(gui._content_window, "height")) >= gui._content_frame.winfo_reqheight())
    first, last = (gui.progress_bars[files[0]], gui.progress_bars[files[-1]])
    gui.root.update()
    assert last.winfo_height() >= 10 and first.winfo_height() >= 10        # every row has real height
    x0, y0, x1, y1 = (float(v) for v in gui._canvas.cget("scrollregion").split())
    assert y1 - y0 >= gui._content_frame.winfo_reqheight() - 2             # the user can scroll to all of it


# ------------------------------------------------------------- a full upload

def test_full_upload_through_the_window(gui, world, tmp_path):
    video = write_mp4(tmp_path / "videos" / "My_Trip-1.mp4")
    gui_text_before = text_of(gui.log_text)
    start_upload(gui, world, [video], title_var="")
    assert gui._uploading and not disabled(gui.cancel_button)
    assert disabled(gui.upload_button) and disabled(gui.sign_in_button)      # no double start
    pump(gui, lambda: idle(gui))

    assert gui.dialogs.error == []
    assert gui.dialogs.info and gui.dialogs.info[-1][0] == "Upload complete"
    assert "1 video(s) uploaded" in gui.dialogs.info[-1][1]
    bar = gui.progress_bars[str(video)]
    assert float(bar["value"]) == float(bar.cget("maximum")) > 0
    assert gui.status_labels[str(video)].cget("text") == "Done"
    assert "https://www.youtube.com/watch?v=fakeVid0001" in text_of(gui.log_text)
    assert world.server.videos["fakeVid0001"]["snippet"]["title"] == "My Trip-1"
    assert not disabled(gui.upload_button) and disabled(gui.cancel_button)
    assert text_of(gui.errors_text).strip() == ""
    assert (tmp_path / "videos" / "My_Trip-1.mp4.url").exists()
    assert gui.account_status_var.get() == "work: not signed in" or "work:" in gui.account_status_var.get()
    assert gui_text_before != text_of(gui.log_text)


def test_worker_thread_never_touches_a_widget(gui, world, tmp_path, monkeypatch):
    """Tk is not thread safe: every widget call must come from the main thread."""
    violations = []
    main_thread = threading.current_thread()

    def guard(cls, name):
        original = getattr(cls, name)

        def wrapper(self, *args, **kwargs):
            if threading.current_thread() is not main_thread:
                violations.append("%s.%s from %s" % (cls.__name__, name, threading.current_thread().name))
            return original(self, *args, **kwargs)
        monkeypatch.setattr(cls, name, wrapper)

    for cls, names in ((tk.Text, ["insert", "delete", "see", "configure"]),
                       (ttk.Progressbar, ["configure", "__setitem__"]),
                       (ttk.Label, ["configure"]),
                       (ttk.Button, ["state"]),
                       (tk.Misc, ["after", "after_idle"]),
                       (tk.Variable, ["set", "get"])):
        for name in names:
            guard(cls, name)

    videos = [write_mp4(tmp_path / "v" / ("clip%d.mp4" % i), size=2 * CHUNK + 10, seed=str(i)) for i in range(3)]
    world.server.inject("PUT", "/session/", status=503)      # force a retry log line too
    start_upload(gui, world, videos)
    pump(gui, lambda: idle(gui))
    assert violations == []
    assert gui.dialogs.info[-1][0] == "Upload complete" and len(world.server.videos) == 3


def test_ui_stays_responsive_while_the_server_is_slow(gui, world, tmp_path):
    video = write_mp4(tmp_path / "slow.mp4", size=4 * CHUNK)
    gate = threading.Event()
    original = world.server._put

    def slow_put(path, body, headers):
        gate.wait(5)
        return original(path, body, headers)
    world.server._put = slow_put
    start_upload(gui, world, [video])
    ticks = {"n": 0}

    def tick():
        ticks["n"] += 1
        if gui._uploading:
            gui.root.after(10, tick)
    gui.root.after(10, tick)
    started = time.time()
    while time.time() - started < 0.5:            # the worker is blocked inside the server
        gui.root.update()
        time.sleep(0.005)
    assert gui._uploading and ticks["n"] >= 10    # the event loop kept running
    gate.set()
    pump(gui, lambda: idle(gui))
    assert gui.dialogs.info[-1][0] == "Upload complete"


def test_progress_bar_advances_before_the_upload_finishes(gui, world, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=3 * GUI_CHUNK + 10)
    seen = []
    original = world.server._put

    def spying_put(path, body, headers):
        response = original(path, body, headers)
        time.sleep(0.05)
        return response
    world.server._put = spying_put
    start_upload(gui, world, [video])
    bar = gui.progress_bars[str(video)]
    while gui._uploading:
        gui.root.update()
        seen.append(float(bar["value"]))
        time.sleep(0.005)
    values = sorted(set(round(v) for v in seen))
    assert any(0 < v < float(bar.cget("maximum")) for v in values), values   # saw partial progress


# ------------------------------------------------------------------- failures

def test_failed_upload_is_reported_in_the_window_not_swallowed(gui, world, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    world.server.inject("POST", "/upload/youtube/v3/videos", status=403, reason="quotaExceeded")
    start_upload(gui, world, [video])
    pump(gui, lambda: idle(gui))
    assert gui.dialogs.info == []
    title, message = gui.dialogs.error[-1]
    assert title == "Upload finished with problems" and "1 of 1" in message and "quotaExceeded" in message
    assert gui.status_labels[str(video)].cget("text") == "Failed"
    assert "quotaExceeded" in text_of(gui.errors_text)
    assert not disabled(gui.upload_button)       # usable again


def test_partial_failure_shows_each_files_outcome(gui, world, tmp_path):
    good = write_mp4(tmp_path / "good.mp4", seed="g")
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all" * 100)
    start_upload(gui, world, [bad, good])
    pump(gui, lambda: idle(gui))
    assert gui.status_labels[str(good)].cget("text") == "Done"
    assert gui.status_labels[str(bad)].cget("text") == "Failed"
    assert "bad.mp4" in text_of(gui.errors_text) and "recognised video container" in text_of(gui.errors_text)
    assert gui.dialogs.error[-1][0] == "Upload finished with problems"
    assert "1 of 2" in gui.dialogs.error[-1][1]


def test_follow_up_problems_are_listed(gui, world, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    world.server.inject("GET", "/youtube/v3/playlists", status=403, reason="forbidden")
    start_upload(gui, world, [video])
    pump(gui, lambda: idle(gui))
    assert gui.playlist_var.get() == video.parent.name            # suggested from the folder
    assert video.parent.name in text_of(gui.errors_text)
    assert gui.status_labels[str(video)].cget("text") == "Done (with problems)"
    assert gui.dialogs.error[-1][0] == "Upload finished with problems"


def test_account_sign_in_failure_is_a_dialog_with_the_reason(gui, world, tmp_path, monkeypatch):
    from youtube_upload.errors import AuthenticationError

    def refuse(*a, **k):
        raise AuthenticationError("Google no longer accepts the saved sign-in.")
    monkeypatch.setattr(auth, "get_resource", refuse)
    video = write_mp4(tmp_path / "v.mp4")
    start_upload(gui, world, [video])
    pump(gui, lambda: idle(gui))
    title, message = gui.dialogs.error[-1]
    assert "no longer accepts" in message
    assert "no longer accepts" in text_of(gui.errors_text)


def test_unexpected_crash_in_the_worker_is_shown_and_the_window_recovers(gui, world, tmp_path, monkeypatch):
    video = write_mp4(tmp_path / "v.mp4")
    monkeypatch.setattr(gui_app.cli_main, "main", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")))
    start_upload(gui, world, [video])
    pump(gui, lambda: idle(gui))
    assert gui.dialogs.error[-1][0] == "Unexpected error" and "kaboom" in gui.dialogs.error[-1][1]
    assert "kaboom" in text_of(gui.errors_text)
    crash_log = paths.app_home() / gui_app.CRASH_LOG_NAME
    assert crash_log.exists() and "kaboom" in crash_log.read_text(encoding="utf-8")
    assert not disabled(gui.upload_button)


def test_exceptions_in_tk_callbacks_are_surfaced(gui):
    try:
        raise ValueError("callback bug")
    except ValueError as exc:
        gui.root.report_callback_exception(type(exc), exc, exc.__traceback__)
    assert "callback bug" in text_of(gui.errors_text)
    assert "callback bug" in (paths.app_home() / gui_app.CRASH_LOG_NAME).read_text(encoding="utf-8")


# ---------------------------------------------------------------- bad inputs

def test_no_video_selected_is_refused_with_a_message(gui):
    gui._upload()
    assert gui.dialogs.error[-1][0] == "Invalid fields" and "Select at least one" in gui.dialogs.error[-1][1]
    assert not gui._uploading


@pytest.mark.parametrize("field,value,fragment", [
    ("publish_at_var", "next tuesday", "ISO 8601"),
    ("secrets_var", "/no/such/secrets.json", "Client secrets does not exist"),
    ("credentials_var", "/no/such/creds.json", "Credentials file does not exist"),
])
def test_invalid_fields_are_refused_before_any_thread_starts(gui, tmp_path, field, value, fragment):
    gui._set_video_selection([str(write_mp4(tmp_path / "v.mp4"))])
    getattr(gui, field).set(value)
    gui._upload()
    assert fragment in gui.dialogs.error[-1][1] and not gui._uploading


def test_missing_thumbnail_and_missing_videos_are_reported(gui, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    gui._set_video_selection([str(video)])
    gui.thumbnail_path = str(tmp_path / "gone.png")
    gui._upload()
    assert "Thumbnail file not found" in gui.dialogs.error[-1][1]
    gui.thumbnail_path = None
    video.unlink()
    gui._upload()
    assert "missing" in gui.dialogs.error[-1][1].lower()


def test_unsupported_files_are_ignored_with_a_log_line(gui, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("hi")
    mp4 = write_mp4(tmp_path / "a.mp4")
    supported, unsupported = gui._set_video_selection([str(notes), str(mp4)])
    assert supported == [str(mp4)] and unsupported == [str(notes)]
    assert "Ignoring unsupported files: notes.txt" in text_of(gui.log_text)


def test_playlist_suggestion_never_overwrites_what_the_user_typed(gui, tmp_path):
    """Regression: _ensure_playlist_default replaced a typed playlist at every upload."""
    a = write_mp4(tmp_path / "Holidays" / "a.mp4")
    gui._set_video_selection([str(a)])
    assert gui.playlist_var.get() == "Holidays"                    # suggested
    gui.playlist_var.set("My own list")                            # the user types
    gui._set_video_selection([str(a)])
    gui._ensure_playlist_default()
    assert gui.playlist_var.get() == "My own list"                 # kept
    other = write_mp4(tmp_path / "Other" / "b.mp4", seed="b")
    gui.playlist_var.set("")                                       # cleared -> suggestion returns
    gui._set_video_selection([str(other)])
    assert gui.playlist_var.get() == "Other"
    gui._set_video_selection([str(write_mp4(tmp_path / "Third" / "c.mp4", seed="c"))])
    assert gui.playlist_var.get() == "Third"                       # still a suggestion: follows the folder
    gui._set_video_selection([])
    assert gui.playlist_var.get() == ""


def test_typed_video_paths_round_trip_including_semicolons(gui, tmp_path):
    odd = write_mp4(tmp_path / "dir;odd" / "a.mp4")
    other = write_mp4(tmp_path / "b.mp4", seed="b")
    gui._set_video_selection([str(odd), str(other)])
    assert gui.video_path_var.get().count(";") >= 2
    gui.video_paths = []
    gui._on_video_path_change()                          # what typing/pasting triggers
    assert gui.video_paths == [str(odd), str(other)]       # ';' inside a folder name survives


# --------------------------------------------------------------------- cancel

def test_cancel_stops_the_upload_and_the_next_run_resumes_it(gui, world, tmp_path):
    video = write_mp4(tmp_path / "v.mp4", size=4 * GUI_CHUNK + 10)
    original = world.server._put
    puts = {"n": 0}

    def put_then_cancel(path, body, headers):
        response = original(path, body, headers)
        puts["n"] += 1
        if puts["n"] == 2:
            gui.cancel_event.set()           # as the Cancel button does
        return response
    world.server._put = put_then_cancel
    start_upload(gui, world, [video])
    pump(gui, lambda: idle(gui))
    assert gui.dialogs.info[-1][0] == "Cancelled"
    assert gui.status_labels[str(video)].cget("text") == "Cancelled"
    assert not world.server.videos
    sessions = list((paths.state_dir() / "sessions").glob("*.json"))
    assert len(sessions) == 1                         # kept for resuming

    world.server._put = original
    gui._upload()
    pump(gui, lambda: idle(gui))
    assert gui.dialogs.info[-1][0] == "Upload complete"
    assert world.server.sessions_started == 1 and len(world.server.videos) == 1   # resumed, not restarted


def test_cancel_button_requests_cancellation_only_while_busy(gui):
    gui._cancel_upload()                 # idle: nothing happens, no error
    assert not gui.cancel_event.is_set()
    gui._uploading = True
    gui._cancel_upload()
    assert gui.cancel_event.is_set()
    gui._uploading = False


def test_cancelling_does_not_rebuild_the_progress_rows_under_the_worker(gui, tmp_path):
    video = write_mp4(tmp_path / "v.mp4")
    gui._set_video_selection([str(video)])
    bar = gui.progress_bars[str(video)]
    gui._uploading = True
    gui._reset_progress_bars()
    assert gui.progress_bars[str(video)] is bar and bar.winfo_exists()
    gui._uploading = False


# ----------------------------------------------------------- sign in / sign out

def test_sign_in_button_runs_the_login_action_for_the_account(gui, world, monkeypatch):
    select_account(gui, world)
    seen = {}

    def fake_login(secrets, creds, **kwargs):
        seen.update(secrets=secrets, creds=creds, open_browser=kwargs["open_browser"])
        Path(creds).write_text(json.dumps({"refresh_token": "fake", "client_id": "x", "client_secret": "y"}))
    monkeypatch.setattr(auth, "login", fake_login)
    gui.open_browser_var.set(False)
    gui._sign_in()
    pump(gui, lambda: idle(gui))
    assert Path(seen["creds"]).name == "work.json" and Path(seen["secrets"]).name == "client_secrets.json"
    assert seen["open_browser"] is False
    assert gui.dialogs.info[-1][0] == "Signed in"
    assert "Signed in: work" in text_of(gui.log_text)


def test_sign_out_asks_first_and_only_then_acts(gui, world, monkeypatch):
    select_account(gui, world)
    calls = []
    monkeypatch.setattr(auth, "logout", lambda creds, revoke=True: calls.append((Path(creds).name, revoke))
                        or auth.LogoutResult(True, True, "Signed out."))
    gui.dialogs.answer = False
    gui._sign_out()
    pump(gui, lambda: idle(gui))
    assert calls == [] and gui.dialogs.asked and "revokes" in gui.dialogs.asked[-1][1]
    gui.dialogs.answer = True
    gui._sign_out()
    pump(gui, lambda: idle(gui))
    assert calls == [("work.json", True)] and gui.dialogs.info[-1][0] == "Signed out"


def test_sign_in_failure_is_shown(gui, world, monkeypatch):
    from youtube_upload.errors import AuthenticationError
    select_account(gui, world)
    monkeypatch.setattr(auth, "login", lambda *a, **k: (_ for _ in ()).throw(
        AuthenticationError("Timed out after 300s waiting for the Google sign-in to finish.")))
    gui._sign_in()
    pump(gui, lambda: idle(gui))
    assert "Timed out" in gui.dialogs.error[-1][1]


def test_account_status_line_follows_the_files(gui, world):
    from tests.test_auth import write_new_format
    select_account(gui, world)
    gui._refresh_account_status()
    assert gui.account_status_var.get() == "work: not signed in"
    write_new_format(Path(world.credentials))
    gui._refresh_account_status()
    assert gui.account_status_var.get() == "work: signed in"
    Path(world.credentials).write_text("junk")
    gui._refresh_account_status()
    assert "unreadable" in gui.account_status_var.get()


def test_account_status_explains_a_missing_client_secrets_file(gui, tmp_path):
    empty = tmp_path / "empty_account"
    empty.mkdir()
    gui.accounts_dir_var.set(str(empty))
    gui._commit_accounts_dir()
    pump(gui, lambda: gui.account_var.get() == "empty_account")
    gui._refresh_account_status()
    assert "client_secrets.json not found" in gui.account_status_var.get()


# ------------------------------------------------------------------- settings

def test_choosing_a_new_accounts_folder_keeps_it_and_saves_settings_inside_it(gui, tmp_path, monkeypatch):
    """Regression: the field used to blank itself and settings went to the home-folder file."""
    folder = tmp_path / "Brand_new_account"
    folder.mkdir()
    monkeypatch.setattr(gui_app.filedialog, "askdirectory", lambda **k: str(folder))
    gui._choose_accounts_dir()
    assert gui.accounts_dir_var.get() == str(folder)
    assert gui.account_var.get() == "Brand_new_account"
    saved = json.loads((folder / "gui_settings.json").read_text(encoding="utf-8"))
    assert saved["accounts_dir"] == str(folder) and saved["account"] == "Brand_new_account"
    pointer = json.loads((Path.home() / ".youtube_upload_gui_settings.json").read_text(encoding="utf-8"))
    assert pointer["accounts_dir"] == str(folder)


def test_settings_round_trip_through_a_restart(root, tmp_path):
    folder = tmp_path / "acc"
    folder.mkdir()
    first = gui_app.UploadGUI(root)
    first.accounts_dir_var.set(str(folder))
    first._commit_accounts_dir()
    root.update()
    first.category_var.set("Music")
    first.privacy_var.set("unlisted")
    first.skip_if_exists_var.set("title")
    first.open_browser_var.set(False)
    first.description_text.insert("1.0", "my description")
    first.theme_var.set("light")
    first._save_settings()
    first._closing = True
    for child in root.winfo_children():
        child.destroy()

    root2 = new_tk_root()
    root2.withdraw()
    try:
        second = gui_app.UploadGUI(root2)
        root2.update()
        assert second.accounts_dir_var.get() == str(folder)
        assert (second.category_var.get(), second.privacy_var.get(), second.skip_if_exists_var.get()) == (
            "Music", "unlisted", "title")
        assert second.open_browser_var.get() is False and second.theme_var.get() == "light"
        assert "my description" in text_of(second.description_text)
    finally:
        root2.destroy()


def test_corrupt_settings_are_quarantined_and_reported_not_silently_ignored(root):
    settings = Path.home() / ".youtube_upload_gui_settings.json"
    settings.write_text("{ this is not json", encoding="utf-8")
    app = gui_app.UploadGUI(root)
    root.update()
    assert "was damaged" in text_of(app.errors_text) and settings.name in text_of(app.errors_text)
    assert list(settings.parent.glob(settings.name + ".corrupt-*"))
    assert app.privacy_var.get() == "public"                       # defaults in effect
    assert app._save_settings() is True                            # and saving works again


def test_unwritable_settings_are_reported_once_not_on_every_keystroke(gui, monkeypatch):
    def fail(path, data):
        raise OSError("read-only file system")
    monkeypatch.setattr(gui_app.paths, "write_private_json", fail)
    assert gui._save_settings() is False
    assert gui._save_settings() is False
    assert text_of(gui.errors_text).count("read-only file system") == 1
    gui._manual_save_settings()
    assert gui.dialogs.error[-1][0] == "Save failed" and "read-only" in gui.dialogs.error[-1][1]


def test_settings_files_are_written_atomically_and_privately(gui, tmp_path):
    folder = tmp_path / "acc2"
    folder.mkdir()
    gui.accounts_dir_var.set(str(folder))
    gui.root.update()
    gui._save_settings()
    assert [p.name for p in folder.iterdir() if p.name.endswith(".tmp")] == []
    if os.name == "posix":
        assert (folder / "gui_settings.json").stat().st_mode & 0o777 == 0o600


def test_legacy_auth_browser_setting_is_ignored_not_misread(root):
    settings = Path.home() / ".youtube_upload_gui_settings.json"
    settings.write_text(json.dumps({"auth_browser": False, "privacy": "private"}), encoding="utf-8")
    app = gui_app.UploadGUI(root)
    root.update()
    assert app.privacy_var.get() == "private"
    assert app.open_browser_var.get() is True                       # the old flag meant something else


# ---------------------------------------------------------------- arguments

def test_arguments_passed_to_the_engine(gui, world, tmp_path):
    select_account(gui, world)
    gui.title_var.set("T")
    gui.tags_var.set("a, b")
    gui.category_var.set("Music")
    gui.privacy_var.set("private")
    gui.skip_if_exists_var.set("hash")
    gui.open_link_var.set(True)
    gui.open_browser_var.set(False)
    gui.description_text.insert("1.0", "d")
    a, b = write_mp4(tmp_path / "a.mp4", seed="a"), write_mp4(tmp_path / "b.mp4", seed="b")
    gui._set_video_selection([str(a), str(b)])
    args = gui._build_upload_args()
    assert args[-2:] == [str(a), str(b)]
    for expected in (["--title", "T"], ["--tags", "a, b"], ["--category", "Music"], ["--privacy", "private"],
                     ["--skip-if-exists", "hash"], ["--description", "d"], ["--accounts-dir", str(world.dir)],
                     ["--account", "work"], ["--title-template", "{title}"]):
        i = args.index(expected[0])
        assert args[i:i + len(expected)] == expected
    assert "--open-link" in args and "--no-browser" in args and "--auth-browser" not in args


def test_skip_if_exists_none_is_not_passed(gui, tmp_path):
    gui.skip_if_exists_var.set("none")
    gui._set_video_selection([str(write_mp4(tmp_path / "a.mp4"))])
    assert "--skip-if-exists" not in gui._build_upload_args()


# ----------------------------------------------------------------- closing

def test_closing_idle_window_saves_and_destroys(root):
    app = gui_app.UploadGUI(root)
    app.category_var.set("Sports")
    app._on_close()
    assert app._closing
    with pytest.raises(tk.TclError):
        root.winfo_exists() and root.update()
    assert "Sports" in (Path.home() / ".youtube_upload_gui_settings.json").read_text(encoding="utf-8")


def test_closing_during_an_upload_asks_first(gui):
    gui._uploading = True
    gui.dialogs.answer = False
    gui._on_close()
    assert gui.dialogs.asked and not gui._closing and not gui.cancel_event.is_set()
    gui.dialogs.answer = True
    gui._on_close()
    assert gui.cancel_event.is_set() and gui._closing
    gui._uploading = False


# -------------------------------------------------------------- process level

def test_gui_module_exits_cleanly_with_a_message_when_there_is_no_display():
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env["PYTHONPATH"] = str(REPO)
    if sys.platform.startswith("win"):
        pytest.skip("Windows always has a desktop session")
    result = subprocess.run([sys.executable, "-m", "youtube_upload.gui_app"], capture_output=True,
                            text=True, env=env, timeout=60, cwd=str(REPO))
    assert result.returncode == 1
    assert "could not be opened" in result.stderr and "Traceback" not in result.stderr


# ------------------------------------------------------ findings from the independent review

def test_typing_an_accounts_folder_does_not_wipe_the_form_or_litter_settings(gui, tmp_path):
    """Regression: every keystroke that formed an existing folder reloaded (reset) the whole form
    and wrote gui_settings.json into each folder along the path."""
    target = tmp_path / "projects" / "channels" / "mychannel"
    target.mkdir(parents=True)
    gui.description_text.insert("1.0", "My carefully written description")
    gui.playlist_var.set("My playlist")
    gui.privacy_var.set("unlisted")
    typed = ""
    for ch in str(target):
        typed += ch
        gui.accounts_dir_var.set(typed)
        gui.root.update()
    assert "My carefully written description" in text_of(gui.description_text)
    assert (gui.playlist_var.get(), gui.privacy_var.get()) == ("My playlist", "unlisted")
    assert not list(tmp_path.rglob("gui_settings.json"))             # nothing written into the path's folders
    gui._commit_accounts_dir()                                        # Enter: now it is deliberate
    assert gui.accounts_dir_var.get() == str(target) and gui.account_var.get() == "mychannel"


def test_leaving_the_field_or_picking_from_the_list_loads_the_folder(gui, tmp_path):
    folder = tmp_path / "acc_x"
    folder.mkdir()
    gui.accounts_dir_var.set(str(folder))
    gui.accounts_dir_combo.event_generate("<FocusOut>")
    gui.root.update()
    assert gui.account_var.get() == "acc_x"


def test_window_starts_when_the_saved_accounts_folder_cannot_be_used(root, tmp_path):
    """Regression: resolving the account called mkdir(); an unusable saved folder made the
    constructor raise on every start."""
    blocker = tmp_path / "a_file_not_a_folder"
    blocker.write_text("x")
    settings = Path.home() / ".youtube_upload_gui_settings.json"
    settings.write_text(json.dumps({"accounts_dir": str(blocker / "accounts"),
                                    "secrets_path": str(tmp_path / "client_secrets.json")}), encoding="utf-8")
    app = gui_app.UploadGUI(root)                       # must not raise
    root.update()
    assert app.account_status_var.get()                  # says something useful instead of crashing


def test_the_status_line_never_creates_directories(gui, tmp_path):
    missing = tmp_path / "nowhere" / "accounts"
    gui.accounts_dir_var.set(str(missing))
    gui._refresh_account_status()
    assert not (tmp_path / "nowhere").exists()


def test_gui_started_from_a_terminal_never_blocks_on_a_playlist_prompt(gui, world, tmp_path, monkeypatch):
    """Regression: the worker called input() when stdin was a terminal; Cancel could not unblock it."""
    import builtins

    class Tty(object):
        def isatty(self):
            return True

        def readline(self):
            raise AssertionError("the worker must never read from the terminal")
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(builtins, "input", lambda *a: (_ for _ in ()).throw(AssertionError("input() called")))
    video = write_mp4(tmp_path / "Holiday_clips" / "v.mp4")
    start_upload(gui, world, [video])
    pump(gui, lambda: idle(gui), timeout=20)
    assert gui.dialogs.error == [] and gui.dialogs.info[-1][0] == "Upload complete"
    assert [p["title"] for p in world.server.playlists.values()] == ["Holiday_clips"]


def test_credentials_file_is_not_shared_between_several_listed_accounts(gui, world, tmp_path):
    select_account(gui, world)
    gui.credentials_var.set(str(world.dir / "work.json"))
    gui.accounts_var.set("work, home")
    args = gui._account_args()
    assert "--credentials-file" not in args and ["--accounts", "work, home"] == args[args.index("--accounts"):args.index("--accounts") + 2]
    assert "ignored because several accounts" in text_of(gui.log_text)
    gui.accounts_var.set("")
    assert "--credentials-file" in gui._account_args()


def test_crash_log_and_panes_are_redacted(gui):
    gui_app.append_crash_log("boom https://u/?upload_id=SECRETUPLOADID&uploadType=resumable")
    text = (paths.app_home() / gui_app.CRASH_LOG_NAME).read_text(encoding="utf-8")
    assert "SECRETUPLOADID" not in text and "[redacted]" in text
    gui._log("retrying https://u/?upload_id=SECRETPANE&x=1")
    gui._log_error("failed https://u/revoke?token=SECRETERRPANE")
    assert "SECRETPANE" not in text_of(gui.log_text) and "SECRETERRPANE" not in text_of(gui.errors_text)
