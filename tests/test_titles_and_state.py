"""Titles (#12), per-user paths, atomic state files, the ledger and small helpers."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.conftest import mode_of
from youtube_upload import lib, paths, titles
from youtube_upload.errors import OptionsError, UploadCancelled
from youtube_upload.ledger import Ledger

REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------------ titles

def sanitize(text):
    notes = []
    return titles.sanitize_title(text, notes.append), notes


def test_hyphens_are_preserved_and_underscores_become_spaces_without_a_warning():
    assert sanitize("2026-01-05_Trip-to_Rome") == ("2026-01-05 Trip-to Rome", [])


def test_whitespace_controls_become_spaces_not_glued_words():
    assert sanitize("a\tb\nc")[0] == "a b c"


def test_removed_characters_are_reported():
    title, notes = sanitize("Fun <b>video</b>")
    assert title == "Fun b video /b" or "<" not in title
    assert "<" not in title and ">" not in title
    assert len(notes) == 1 and "removed" in notes[0]


def test_control_characters_are_reported():
    title, notes = sanitize("bad\x00name\x07")
    assert title == "badname" and notes and "removed" in notes[0]


def test_long_titles_are_truncated_with_a_warning():
    title, notes = sanitize("x" * 150)
    assert len(title) == 100 and notes and "truncated" in notes[0]


def test_empty_names_fall_back_to_untitled_with_a_warning():
    for blank in ("", "   ", "___", "\t\n", "<>"):
        title, notes = sanitize(blank)
        assert title == "Untitled", blank
        assert notes and "Untitled" in notes[-1], blank


def test_unicode_is_kept():
    assert sanitize("Café 日本語 \U0001F600")[0] == "Café 日本語 \U0001F600"


def test_default_title_from_path_uses_the_stem_only():
    assert titles.default_title_from_video_path("/a/b/My_Movie.final.mp4") == "My Movie.final"


def test_explicit_titles_are_kept_as_typed_except_hard_limits():
    notes = []
    assert titles.explicit_title("  My_Title - draft  ", notes.append) == "My_Title - draft"
    assert notes == []
    assert titles.explicit_title("a<b>c", notes.append) == "a b c" and notes
    with pytest.raises(OptionsError):
        titles.explicit_title("<>")
    long = titles.explicit_title("y" * 300, notes.append)
    assert len(long) == 100


# ------------------------------------------------------------------------- paths

def test_app_home_honours_the_override_and_platform_conventions(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_UPLOAD_HOME", str(tmp_path / "custom"))
    assert paths.app_home() == tmp_path / "custom"
    monkeypatch.delenv("YOUTUBE_UPLOAD_HOME")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    expected = tmp_path / ("roaming" if os.name == "nt" else "xdg") / "youtube-upload"
    assert paths.app_home() == expected
    assert REPO not in paths.app_home().parents   # never inside the source tree


def test_default_accounts_dir_prefers_an_existing_legacy_folder(isolated_home):
    assert paths.default_accounts_dir() == paths.app_home() / "accounts"
    (isolated_home / ".youtube-upload-accounts").mkdir()
    assert paths.default_accounts_dir() == isolated_home / ".youtube-upload-accounts"


@pytest.mark.parametrize("label", ["work", "General_account", "a.b", "my channel", "x-1", "Électronique", "a" * 64])
def test_good_labels(label):
    assert paths.validate_account_label(label) == label


@pytest.mark.parametrize("label", ["", " ", "../x", "a/b", "a\\b", ".", "..", ".x", "x.", "x ", " x", "a:b",
                                   "a*b", "a?b", "a|b", 'a"b', "con", "COM1", "lpt3.txt", "a" * 65, "a\nb", None, 5])
def test_bad_labels(label):
    with pytest.raises(OptionsError):
        paths.validate_account_label(label)


def test_private_write_is_atomic_and_owner_only(tmp_path):
    target = tmp_path / "deep" / "dir" / "token.json"
    paths.write_private_json(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    if os.name == "posix":
        assert mode_of(target) == 0o600
    paths.write_private_json(target, {"a": 2})
    assert json.loads(target.read_text()) == {"a": 2}
    assert [p.name for p in target.parent.iterdir()] == ["token.json"]   # no temp files left


def test_failed_write_keeps_the_old_file_and_leaves_no_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "w" / "token.json"
    paths.write_private_text(target, "OLD")

    def explode(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(paths.os, "replace", explode)
    with pytest.raises(OSError):
        paths.write_private_text(target, "NEW")
    assert target.read_text() == "OLD"
    assert [p.name for p in target.parent.iterdir()] == ["token.json"]


def test_corrupt_state_file_is_moved_aside_not_deleted(tmp_path):
    f = tmp_path / "state.json"
    f.write_text("{ nope")
    warnings = []
    assert paths.read_json_or_quarantine(f, warn=warnings.append) == {}
    assert not f.exists()
    (aside,) = tmp_path.glob("state.json.corrupt-*")
    assert aside.read_text() == "{ nope"
    assert warnings and "corrupt" in warnings[0]
    assert paths.read_json_or_quarantine(tmp_path / "missing.json") == {}
    f.write_text("[1, 2]")
    assert paths.read_json_or_quarantine(f) == {}      # valid JSON but not an object


# ------------------------------------------------------------------------ ledger

def test_ledger_records_reads_and_forgets(tmp_path):
    ledger = Ledger(tmp_path, "UCabc123")
    assert ledger.get("h1") is None
    ledger.record("h1", "vid00000001", "Title", 10, "a.mp4")
    ledger.record("h2", "vid00000002", "Other", 20, "b.mp4")
    assert ledger.get("h1")["video_id"] == "vid00000001"
    ledger.forget("h1")
    assert ledger.get("h1") is None and ledger.get("h2")["title"] == "Other"
    if os.name == "posix":
        assert mode_of(ledger.path) == 0o600


def test_ledgers_are_isolated_per_channel(tmp_path):
    a, b = Ledger(tmp_path, "UCaaa"), Ledger(tmp_path, "UCbbb")
    a.record("h", "vid00000001", "t", 1, "a.mp4")
    assert b.get("h") is None and a.path != b.path


def test_ledger_survives_a_corrupt_file(tmp_path):
    ledger = Ledger(tmp_path, "UCabc")
    ledger.record("h1", "vid00000001", "t", 1, "a.mp4")
    ledger.path.write_text("garbage")
    assert ledger.get("h1") is None
    ledger.record("h2", "vid00000002", "t", 1, "b.mp4")
    assert ledger.get("h2") and list(tmp_path.glob("*.corrupt-*"))


def test_channel_id_cannot_escape_the_directory(tmp_path):
    ledger = Ledger(tmp_path / "ledger", "../../evil")
    ledger.record("h", "vid00000001", "t", 1, "a.mp4")
    assert ledger.path.parent == tmp_path / "ledger"
    assert not (tmp_path / "evil.json").exists()


def test_concurrent_threads_lose_no_entries(tmp_path):
    def work(n):
        ledger = Ledger(tmp_path, "UCthreads")
        for i in range(10):
            ledger.record("h%d_%d" % (n, i), "vid%08d" % (n * 100 + i), "t", 1, "x.mp4")
    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads(Ledger(tmp_path, "UCthreads").path.read_text())
    assert len(data["videos"]) == 60


def test_concurrent_processes_lose_no_entries(tmp_path):
    script = (
        "import sys; from youtube_upload.ledger import Ledger\n"
        "n = sys.argv[2]; led = Ledger(sys.argv[1], 'UCprocs')\n"
        "[led.record('h%s_%d' % (n, i), 'vid%08d' % (int(n) * 100 + i), 't', 1, 'x.mp4') for i in range(8)]\n")
    env = dict(os.environ, PYTHONPATH=str(REPO))
    procs = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(n)], env=env,
                              stderr=subprocess.PIPE) for n in range(5)]
    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err.decode()
    assert len(json.loads(Ledger(tmp_path, "UCprocs").path.read_text())["videos"]) == 40


def test_stale_lock_from_a_crashed_process_is_broken(tmp_path):
    ledger = Ledger(tmp_path, "UCstale")
    lock = ledger.path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    ledger.record("h", "vid00000001", "t", 1, "x.mp4")
    assert ledger.get("h")


def test_live_lock_times_out_with_a_clear_error(tmp_path, monkeypatch):
    from youtube_upload import ledger as ledger_mod
    ledger = Ledger(tmp_path, "UCbusy")
    lock = ledger.path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    ticks = iter(range(0, 1000))
    monkeypatch.setattr(ledger_mod.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError, match="another upload"):
        with ledger_mod._file_lock(lock, timeout=3, stale_after=10 ** 9, sleep=lambda s: None,
                                   clock=lambda: next(ticks)):
            pass


# ---------------------------------------------------------------------- lib helpers

def test_string_to_dict():
    assert lib.string_to_dict(None) is None and lib.string_to_dict("") is None
    assert lib.string_to_dict("a=1, b = 2") == {"a": "1", "b": "2"}
    assert lib.string_to_dict("url=http://x/?a=b") == {"url": "http://x/?a=b"}
    for bad in ("novalue", "=1"):
        with pytest.raises(ValueError):
            lib.string_to_dict(bad)


def test_sha256_file_matches_hashlib_and_is_cancellable(tmp_path):
    import hashlib
    f = tmp_path / "f.bin"
    f.write_bytes(os.urandom(300000))
    assert lib.sha256_file(f, chunk_size=4096) == hashlib.sha256(f.read_bytes()).hexdigest()
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3
    with pytest.raises(UploadCancelled):
        lib.sha256_file(f, cancel_check=cancel, chunk_size=4096)


def test_debug_tolerates_missing_or_broken_stderr(capsys):
    lib.debug("hello")
    assert "hello" in capsys.readouterr().err

    class Broken(object):
        def write(self, text):
            raise UnicodeEncodeError("ascii", "x", 0, 1, "boom")
    lib.debug("café", fd=Broken())          # must not raise
    saved, sys.stderr = sys.stderr, None
    try:
        lib.debug("no stderr under pythonw")      # must not raise
    finally:
        sys.stderr = saved


# ------------------------------------------------- Windows: replace() can be blocked briefly

def test_replace_is_retried_when_another_process_briefly_holds_the_file(tmp_path, monkeypatch):
    """Windows antivirus/indexers often hold a freshly written file for a few ms."""
    target = tmp_path / "token.json"
    paths.write_private_text(target, "OLD")
    real_replace, calls = os.replace, {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "The process cannot access the file because it is being used")
        return real_replace(src, dst)
    monkeypatch.setattr(paths.os, "replace", flaky)
    monkeypatch.setattr(paths.time, "sleep", lambda s: None)
    paths.write_private_text(target, "NEW")
    assert target.read_text() == "NEW" and calls["n"] == 3
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_replace_gives_up_eventually_and_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "w" / "token.json"
    paths.write_private_text(target, "OLD")
    monkeypatch.setattr(paths.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError(13, "locked")))
    monkeypatch.setattr(paths.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        paths.write_private_text(target, "NEW")
    assert target.read_text() == "OLD" and [p.name for p in target.parent.iterdir()] == ["token.json"]
