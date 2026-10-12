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
