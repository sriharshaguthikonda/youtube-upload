import os
import re

from . import formats

# A ";" separates two paths only when the next one visibly starts a new path:
# a drive letter ("C:\\", "D:/"), a UNC share ("\\\\server"), a POSIX root, "~" or a quote.
# This keeps ";" inside a directory or file name intact.
_SEPARATOR = re.compile(r"""\s*;\s*(?=(?:[A-Za-z]:[\\/]|\\\\|/|~|["']))""")


def parse_video_paths(text):
    """Split what a user typed or pasted into a list of file paths.

    Accepts one path per line and/or paths separated by ";", with optional
    surrounding quotes. Works for Windows (drive letters, UNC) and POSIX paths.
    """
    paths = []
    for line in (text or "").replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        for chunk in _SEPARATOR.split(line):
            chunk = chunk.strip()
            if len(chunk) >= 2 and chunk[0] == chunk[-1] and chunk[0] in "\"'":
                chunk = chunk[1:-1].strip()
            if chunk:
                paths.append(chunk)
    return paths


def join_video_paths(paths):
    """Inverse of :func:`parse_video_paths` for display in a single-line entry."""
    return "; ".join(paths)


def filter_supported_video_paths(paths):
    """
    Split paths into supported and unsupported based on configured extensions.
    Returns (supported_list, unsupported_list).
    """
    supported = []
    unsupported = []
    for p in paths:
        if formats.is_supported_extension(p):
            supported.append(p)
        else:
            unsupported.append(p)
    return supported, unsupported


_DIGITS = re.compile(r"(\d+)")


def natural_key(path):
    """Sort key: case-insensitive, and ``clip2`` before ``clip10``."""
    return [(0, int(part)) if part.isdigit() else (1, part.casefold()) for part in _DIGITS.split(str(path))]


class ScanResult(object):
    """What :func:`scan_folder` found."""

    def __init__(self):
        self.videos = []        # supported files, natural order
        self.ignored = 0        # files with an unsupported extension
        self.errors = []        # folders that could not be read (text, already readable)
        self.cancelled = False


def scan_folder(folder, cancelled=lambda: False):
    """Find every supported video under ``folder`` (blocking: run it off the UI thread).

    Symbolic links to folders are not followed (no loops), unreadable sub-folders are reported
    instead of aborting the scan, and the order does not depend on the file system.
    """
    result = ScanResult()

    def on_error(exc):
        result.errors.append("{0}: {1}".format(getattr(exc, "filename", None) or folder,
                                               getattr(exc, "strerror", None) or exc))
    for root, _dirs, files in os.walk(str(folder), onerror=on_error):
        if cancelled():
            result.cancelled = True
            break
        for name in files:
            path = os.path.join(root, name)
            if formats.is_supported_extension(path):
                result.videos.append(path)
            else:
                result.ignored += 1
    result.videos.sort(key=natural_key)
    return result
