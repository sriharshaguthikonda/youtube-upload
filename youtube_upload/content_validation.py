"""Lightweight, best-effort content validation for video files."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import main


def _read_head(path: Path, size: int = 512) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def _starts_with(data: bytes, prefix: bytes) -> bool:
    return data.startswith(prefix)


def _contains_at(data: bytes, needle: bytes, offset: int) -> bool:
    return len(data) >= offset + len(needle) and data[offset:offset + len(needle)] == needle


def _check_mp4_like(data: bytes) -> bool:
    # MP4/QuickTime/3GP should contain an ftyp box near the beginning.
    return _contains_at(data, b"ftyp", 4)


def _check_webm(data: bytes) -> bool:
    return _starts_with(data, b"\x1a\x45\xdf\xa3")


def _check_flv(data: bytes) -> bool:
    return _starts_with(data, b"FLV")


def _check_mpeg_ps(data: bytes) -> bool:
    return _starts_with(data, b"\x00\x00\x01\xba")


def _check_avi(data: bytes) -> bool:
    return _starts_with(data, b"RIFF") and _contains_at(data, b"AVI ", 8)


def _check_asf_wmv(data: bytes) -> bool:
    return _starts_with(data, b"\x30\x26\xb2\x75\x8e\x66\xcf\x11")


_SIGNATURE_CHECKS: dict[str, Callable[[bytes], bool]] = {
    # MP4/QuickTime family
    "mp4": _check_mp4_like,
    "mpeg4": _check_mp4_like,
    "mov": _check_mp4_like,
    "3gp": _check_mp4_like,
    "3gpp": _check_mp4_like,
    # WebM
    "webm": _check_webm,
    # FLV
    "flv": _check_flv,
    # MPEG Program Stream
    "mpeg": _check_mpeg_ps,
    "mpeg1": _check_mpeg_ps,
    "mpeg2": _check_mpeg_ps,
    "mpg": _check_mpeg_ps,
    "mpegps": _check_mpeg_ps,
    # AVI
    "avi": _check_avi,
    # ASF/WMV
    "wmv": _check_asf_wmv,
}


def validate_video_content(video_path: str, minimum_size_bytes: int = 1024) -> None:
    """
    Ensure the file has video-like content and matches its container signature.
    Raises main.InvalidVideoFormat on failure.
    """
    path = Path(video_path)
    if not path.exists():
        raise main.InvalidVideoFormat(f"File does not exist: {video_path}")
    if path.stat().st_size < minimum_size_bytes:
        raise main.InvalidVideoFormat(f"File too small to be a valid video: {video_path}")

    suffix = path.suffix.lower().lstrip(".")
    data = _read_head(path)
    checker = _SIGNATURE_CHECKS.get(suffix)
    if checker:
        if not checker(data):
            raise main.InvalidVideoFormat(
                f"Content of '{video_path}' does not look like a valid {suffix.upper()} video."
            )
    else:
        # For less common extensions (hevc, h265, prores, cineform, dnxhr) perform a generic sanity check.
        if data.startswith(b"\x00\x00\x00\x00") or all(b == 0x00 for b in data[:16]):
            raise main.InvalidVideoFormat(
                f"Content of '{video_path}' does not appear to contain valid video data."
            )


__all__ = ["validate_video_content"]
