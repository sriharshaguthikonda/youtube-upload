"""Pure-Python helpers behind the GUI's file selection (no Tk needed)."""
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
