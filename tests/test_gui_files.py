"""Pure-Python helpers behind the GUI's file selection (no Tk needed)."""
import os

import pytest

from youtube_upload.gui_app_files import (filter_supported_video_paths, join_video_paths,
                                          parse_video_paths)


@pytest.mark.parametrize("text,expected", [
    (r"C:\a b\one.mp4; D:\two.mp4", [r"C:\a b\one.mp4", r"D:\two.mp4"]),
    (r"C:\dir;with\semi.mp4", [r"C:\dir;with\semi.mp4"]),                      # ';' inside a name survives
    (r"C:\a.mp4; \\server\share\b.mp4", [r"C:\a.mp4", r"\\server\share\b.mp4"]),  # UNC
    ("C:/a.mp4; D:/b.mp4", ["C:/a.mp4", "D:/b.mp4"]),                          # forward slashes
    ('"C:\\a b.mp4"; "D:\\c d.mp4"', [r"C:\a b.mp4", r"D:\c d.mp4"]),           # quotes
    ("/home/u/a.mp4; /home/u/b.mp4\n~/c.mp4", ["/home/u/a.mp4", "/home/u/b.mp4", "~/c.mp4"]),
    ("  \n \n", []), ("", []), (None, []),
    ("C:\\a.mp4\r\nD:\\b.mp4\r\n", [r"C:\a.mp4", r"D:\b.mp4"]),
])
def test_parse_video_paths(text, expected):
    assert parse_video_paths(text) == expected


def test_join_then_parse_round_trips():
    original = [r"C:\Users\me\My Videos\a.mp4", r"D:\x;y\b.mp4", r"\\nas\media\c.mp4"]
    assert parse_video_paths(join_video_paths(original)) == original


def test_filter_supported_video_paths():
    supported, unsupported = filter_supported_video_paths(["a.MP4", "b.txt", "c", "d.webm", "e.mkv"])
    assert supported == ["a.MP4", "d.webm"] and unsupported == ["b.txt", "c", "e.mkv"]


# ------------------------------------------------------------------ folder scanning

def _touch(path, size=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return str(path)


def test_scan_folder_returns_only_supported_videos_in_natural_order(tmp_path):
    from youtube_upload.gui_app_files import scan_folder
    wanted = [_touch(tmp_path / "clip1.mp4"), _touch(tmp_path / "clip2.mp4"), _touch(tmp_path / "clip10.mp4"),
              _touch(tmp_path / "sub" / "a.webm")]
    _touch(tmp_path / "notes.txt")
    _touch(tmp_path / "sub" / "cover.jpg")
    result = scan_folder(tmp_path)
    assert result.videos == wanted                      # natural order: clip2 before clip10, then sub/
    assert result.ignored == 2 and result.errors == [] and not result.cancelled


def test_scan_folder_is_stable_whatever_the_file_system_order(tmp_path, monkeypatch):
    import os
    from youtube_upload import gui_app_files
    names = ["b.mp4", "a.mp4", "C.mp4"]
    for name in names:
        _touch(tmp_path / name)
    real = os.walk
    monkeypatch.setattr(gui_app_files.os, "walk",
                        lambda *a, **k: ((root, list(reversed(dirs)), list(reversed(files)))
                                         for root, dirs, files in real(*a, **k)))
    assert [os.path.basename(p) for p in gui_app_files.scan_folder(tmp_path).videos] == ["a.mp4", "b.mp4", "C.mp4"]


def test_scan_folder_reports_unreadable_subfolders_without_losing_the_rest(tmp_path, monkeypatch):
    import os
    from youtube_upload import gui_app_files
    good = _touch(tmp_path / "ok.mp4")
    real = os.walk

    def walking(top, onerror=None, **kw):
        for item in real(top, onerror=onerror, **kw):
            yield item
        if onerror:
            onerror(PermissionError(13, "Permission denied", str(tmp_path / "locked")))
    monkeypatch.setattr(gui_app_files.os, "walk", walking)
    result = gui_app_files.scan_folder(tmp_path)
    assert result.videos == [good]
    assert len(result.errors) == 1 and "locked" in result.errors[0]


def test_scan_folder_can_be_cancelled(tmp_path):
    from youtube_upload.gui_app_files import scan_folder
    for i in range(30):
        _touch(tmp_path / ("d%d" % i) / "v.mp4")
    seen = {"n": 0}

    def cancelled():
        seen["n"] += 1
        return seen["n"] > 3
    result = scan_folder(tmp_path, cancelled=cancelled)
    assert result.cancelled and len(result.videos) < 30


def test_scan_folder_does_not_follow_symlink_loops(tmp_path):
    import os
    from youtube_upload.gui_app_files import scan_folder
    _touch(tmp_path / "real" / "v.mp4")
    try:
        os.symlink(str(tmp_path), str(tmp_path / "real" / "loop"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    assert len(scan_folder(tmp_path).videos) == 1
