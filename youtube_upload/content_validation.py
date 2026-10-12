"""Check that a file really is a video container before spending an upload on it (#14).

Two independent gates:

1. **Signature gate** (always): the first bytes must have the exact structure
   of a known container. This replaces the old ``b"ftyp" in first_4096_bytes``
   substring test, which accepted a file of zeros with the word ``ftyp``
   anywhere in the header and rejected valid raw HEVC streams (which legally
   begin with four zero bytes).
2. **ffprobe gate** (when ``ffprobe`` is installed): it must find at least one
   video stream. Set ``YOUTUBE_UPLOAD_NO_FFPROBE=1`` to skip it.

Neither gate proves the whole file decodes; YouTube can still reject a damaged
file after upload. They stop obvious mistakes (text files, empty or truncated
files, wrong extension) before a multi-GB transfer.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from .errors import InvalidVideoFormat

HEADER_BYTES = 64
FFPROBE_TIMEOUT = 30

_ISO_FIRST_BOXES_MP4 = (b"ftyp",)
# QuickTime files may start with other atoms than ftyp.
_ISO_FIRST_BOXES_MOV = (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot")
_ASF_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_MXF_KEY = bytes.fromhex("060e2b34020501010d0102")


def _read_head(path, size=HEADER_BYTES):
    with Path(path).open("rb") as handle:
        return handle.read(size)


def _is_isobmff(head, first_boxes):
    """ISO base media (MP4, 3GP, QuickTime): [size:4][type:4]..."""
    if len(head) < 12 or head[4:8] not in first_boxes:
        return False
    size = int.from_bytes(head[:4], "big")
    box = head[4:8]
    if size == 1:                      # 64-bit size follows
        return True
    if box == b"ftyp":
        brand_ok = all(32 <= b < 127 for b in head[8:12])
        return brand_ok and (size == 0 or size >= 16)
    return size == 0 or size >= 8


def _is_matroska(head):
    return head.startswith(b"\x1a\x45\xdf\xa3")


def _is_flv(head):
    return head[:4] == b"FLV\x01" and head[5:9] == b"\x00\x00\x00\x09"


def _is_mpeg(head):
    # 00 00 01 BA = program stream pack header, B3 = sequence header (elementary stream)
    return head[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")


def _is_avi(head):
    return head[:4] == b"RIFF" and head[8:12] in (b"AVI ", b"AVIX")


def _is_asf(head):
    return head[:16] == _ASF_GUID


def _is_mxf(head):
    return head[:11] == _MXF_KEY


def _is_hevc_annexb(head):
    """Raw H.265: Annex-B start code then a VPS/SPS/PPS/AUD/SEI NAL unit."""
    for prefix in (b"\x00\x00\x00\x01", b"\x00\x00\x01"):
        if head.startswith(prefix) and len(head) > len(prefix) + 1:
            nal0, nal1 = head[len(prefix)], head[len(prefix) + 1]
            nal_type = (nal0 >> 1) & 0x3F
            return nal0 & 0x80 == 0 and (nal1 & 0x07) != 0 and 32 <= nal_type <= 40
    return False


_CONTAINER_CHECKS = [
    ("mp4", lambda h: _is_isobmff(h, _ISO_FIRST_BOXES_MP4)),
    ("quicktime", lambda h: _is_isobmff(h, _ISO_FIRST_BOXES_MOV)),
    ("matroska/webm", _is_matroska),
    ("flv", _is_flv),
    ("mpeg", _is_mpeg),
    ("avi", _is_avi),
    ("asf/wmv", _is_asf),
    ("mxf", _is_mxf),
    ("hevc", _is_hevc_annexb),
]
_CHECKS = dict(_CONTAINER_CHECKS)

# Extension -> containers it may legitimately hold (the first is the usual one).
_EXPECTED = {
    "mp4": ["mp4"], "mpeg4": ["mp4"], "3gp": ["mp4"], "3gpp": ["mp4"],
    "mov": ["quicktime", "mp4"],
    "webm": ["matroska/webm"],
    "flv": ["flv"],
    "mpeg": ["mpeg"], "mpeg1": ["mpeg"], "mpeg2": ["mpeg"], "mpg": ["mpeg"], "mpegps": ["mpeg"],
    "avi": ["avi"],
    "wmv": ["asf/wmv"],
    # codec names used as extensions: raw stream or any container that carries them
    "hevc": ["hevc", "mp4", "quicktime", "matroska/webm", "mxf"],
    "h265": ["hevc", "mp4", "quicktime", "matroska/webm", "mxf"],
    "prores": ["quicktime", "mp4", "mxf", "matroska/webm"],
    "dnxhr": ["mxf", "quicktime", "mp4", "avi", "matroska/webm"],
    "cineform": ["quicktime", "avi", "mp4", "matroska/webm"],
}


def detect_container(head):
    """Name of the first known container whose signature matches, else ``None``."""
    for name, check in _CONTAINER_CHECKS:
        if check(head):
            return name
    return None


def _ffprobe_enabled():
    return not os.environ.get("YOUTUBE_UPLOAD_NO_FFPROBE") and shutil.which("ffprobe") is not None


def _ffprobe_has_video(path):
    """``True``/``False`` from ffprobe, ``None`` if it could not run to completion."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
           "stream=codec_type", "-of", "json", "-i", str(Path(path).resolve())]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=FFPROBE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    if done.returncode != 0:
        first = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return False, (first[0] if first else "ffprobe could not read the file")
    try:
        streams = json.loads(done.stdout.decode("utf-8", "replace")).get("streams") or []
    except ValueError:
        return None, ""
    return any(s.get("codec_type") == "video" for s in streams), "no video stream found"


def validate_video_content(video_path, minimum_size_bytes=1024, warn=None):
    """Raise :class:`InvalidVideoFormat` unless ``video_path`` looks like a real video.

    Returns the detected container name. A recognised container under the
    "wrong" extension is accepted (YouTube sniffs content) but ``warn`` is
    told, because it usually means a renamed file.
    """
    path = Path(video_path)
    if not path.exists():
        raise InvalidVideoFormat("File does not exist: {0}".format(video_path))
    if not path.is_file():
        raise InvalidVideoFormat("Not a regular file: {0}".format(video_path))
    try:
        size = path.stat().st_size
        head = _read_head(path)
    except OSError as exc:
        raise InvalidVideoFormat("Cannot read {0}: {1}".format(video_path, exc)) from exc
    if size < minimum_size_bytes:
        raise InvalidVideoFormat("File too small to be a valid video: {0}".format(video_path))

    suffix = path.suffix.lower().lstrip(".")
    expected = _EXPECTED.get(suffix)
    detected = None
    if expected:
        for name in expected:
            if _CHECKS[name](head):
                detected = name
                break
    if detected is None:
        detected = detect_container(head)
        if detected is None:
            raise InvalidVideoFormat(
                "Content of '{0}' is not a recognised video container (expected {1}).".format(
                    video_path, "/".join(expected) if expected else "a video file"))
        if warn:
            warn("'{0}' looks like {1} but has the extension .{2}; uploading it anyway.".format(
                path.name, detected, suffix))

    if _ffprobe_enabled():
        has_video, detail = _ffprobe_has_video(path)
        if has_video is False:
            raise InvalidVideoFormat("ffprobe found no usable video in '{0}': {1}".format(video_path, detail))
    return detected


__all__ = ["validate_video_content", "detect_container"]
