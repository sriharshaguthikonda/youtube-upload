"""#14: strict container checks, tested with real media where ffmpeg is available."""
import os
import shutil
import subprocess

import pytest

from tests.helpers import MP4_HEADER
from youtube_upload import content_validation as cv
from youtube_upload.errors import InvalidVideoFormat

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_FFPROBE = shutil.which("ffprobe") is not None


def _make(tmp_path_factory, name, *codec_args):
    out = tmp_path_factory.getbasetemp() / "media"
    out.mkdir(exist_ok=True)
    target = out / name
    if not target.exists():
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=128x96:rate=10",
               "-pix_fmt", "yuv420p"] + list(codec_args) + [str(target)]
        done = subprocess.run(cmd, capture_output=True, timeout=60)
        if done.returncode != 0 or not target.exists() or target.stat().st_size < 1024:
            return None
    return target


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    if not HAVE_FFMPEG:
        return {}
    spec = {
        "mp4": ("clip.mp4", "-c:v", "mpeg4"),
        "mov": ("clip.mov", "-c:v", "mpeg4"),
        "3gp": ("clip.3gp", "-c:v", "mpeg4", "-f", "3gp"),
        "avi": ("clip.avi", "-c:v", "mpeg4"),
        "flv": ("clip.flv", "-c:v", "flv"),
        "mpg": ("clip.mpg", "-c:v", "mpeg1video"),
        "wmv": ("clip.wmv", "-c:v", "wmv2"),
        "webm": ("clip.webm", "-c:v", "libvpx"),
        "hevc": ("clip.hevc", "-c:v", "libx265", "-f", "hevc"),
    }
    found = {}
    for key, (name, *args) in spec.items():
        path = _make(tmp_path_factory, name, *args)
        if path is not None:
            found[key] = path
    return found


@pytest.fixture
def with_ffprobe(monkeypatch):
    monkeypatch.delenv("YOUTUBE_UPLOAD_NO_FFPROBE", raising=False)


def test_real_files_pass_the_signature_gate(samples):
    if not samples:
        pytest.skip("ffmpeg not available")
    expected = {"mp4": "mp4", "mov": "quicktime", "3gp": "mp4", "avi": "avi", "flv": "flv",
                "mpg": "mpeg", "wmv": "asf/wmv", "webm": "matroska/webm", "hevc": "hevc"}
    for key, path in samples.items():
        detected = cv.validate_video_content(str(path))
        assert detected in (expected[key], "mp4", "quicktime"), (key, detected)
    assert {"mp4", "avi", "flv"} <= set(samples)   # the common ones must have been generated


@pytest.mark.skipif(not HAVE_FFPROBE, reason="ffprobe not installed")
def test_real_files_pass_ffprobe_too(samples, with_ffprobe):
    for path in samples.values():
        cv.validate_video_content(str(path))


@pytest.mark.skipif(not HAVE_FFPROBE, reason="ffprobe not installed")
def test_valid_header_but_no_video_is_rejected_by_ffprobe(tmp_path, with_ffprobe):
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(MP4_HEADER + b"\x01" * 4000)
    with pytest.raises(InvalidVideoFormat, match="ffprobe"):
        cv.validate_video_content(str(fake))


@pytest.mark.skipif(not HAVE_FFPROBE, reason="ffprobe not installed")
def test_truncated_real_file_is_rejected_by_ffprobe(samples, tmp_path, with_ffprobe):
    if "mp4" not in samples:
        pytest.skip("no mp4 sample")
    data = samples["mp4"].read_bytes()
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(data[: min(len(data) // 2, 2000)])
    with pytest.raises(InvalidVideoFormat):
        cv.validate_video_content(str(cut))


def test_ffprobe_failure_to_run_does_not_block_a_good_signature(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_UPLOAD_NO_FFPROBE", raising=False)
    monkeypatch.setattr(cv.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no exec")))
    monkeypatch.setattr(cv.shutil, "which", lambda name: "/usr/bin/ffprobe")
    good = tmp_path / "ok.mp4"
    good.write_bytes(MP4_HEADER + b"\x01" * 4000)
    assert cv.validate_video_content(str(good)) == "mp4"


# -------------------------------------------------- the exact false accepts of #14

def test_zeros_with_ftyp_text_inside_the_first_4kb_are_rejected(tmp_path):
    """Old check: b"ftyp" in first 4096 bytes. A file of zeros with that word passed."""
    p = tmp_path / "zeros.mp4"
    p.write_bytes(b"\x00" * 100 + b"ftyp" + b"\x00" * 3000)
    with pytest.raises(InvalidVideoFormat, match="not a recognised video container"):
        cv.validate_video_content(str(p))


def test_text_file_with_ftyp_word_is_rejected(tmp_path):
    p = tmp_path / "doc.mp4"
    p.write_bytes(b"The word ftyp appears here. " * 100)
    with pytest.raises(InvalidVideoFormat):
        cv.validate_video_content(str(p))


@pytest.mark.parametrize("name,content", [
    ("a.mp4", b"\x00" * 5000),
    ("a.avi", b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 3000),         # a WAV, not an AVI
    ("a.flv", b"FLV\x02" + b"\x00" * 3000),                               # wrong version
    ("a.mpg", b"\x00\x00\x01\x00" + b"\x01" * 3000),                       # picture start, not a stream header
    ("a.wmv", b"\x30\x26\xb2\x75" + b"\x00" * 3000),                       # truncated ASF GUID
    ("a.webm", b"\x1a\x45\xdf" + b"\x00" * 3000),                          # truncated EBML magic
    ("a.hevc", b"\x00\x00\x00\x01\xff\xff" + b"\x00" * 3000),              # start code, junk NAL
    ("a.prores", os.urandom(4000)),
    ("a.dnxhr", b"\x00" * 4000),
    ("a.mp4", b"\x00\x00\x00\x04ftypmp42" + b"\x00" * 3000),               # ftyp box size smaller than header
    ("a.mp4", b"\x00\x00\x00\x18ftyp\x01\x02\x03\x04" + b"\x00" * 3000),   # non-printable brand
])
def test_wrong_structure_is_rejected(tmp_path, name, content):
    p = tmp_path / name
    p.write_bytes(content)
    with pytest.raises(InvalidVideoFormat):
        cv.validate_video_content(str(p))


def test_raw_hevc_stream_starting_with_zero_bytes_is_accepted(tmp_path):
    """The old fallback rejected any file starting with four zero bytes, which is valid Annex-B HEVC."""
    p = tmp_path / "raw.hevc"
    p.write_bytes(b"\x00\x00\x00\x01\x40\x01\x0c\x01\xff\xff" + b"\x11" * 3000)
    assert cv.validate_video_content(str(p)) == "hevc"
    q = tmp_path / "raw.h265"
    q.write_bytes(b"\x00\x00\x01\x42\x01\x01" + b"\x11" * 3000)   # 3-byte start code + SPS
    assert cv.validate_video_content(str(q)) == "hevc"


@pytest.mark.parametrize("name,header,expected", [
    ("a.mpg", b"\x00\x00\x01\xba" + b"\x44" * 20, "mpeg"),
    ("a.mpeg", b"\x00\x00\x01\xb3" + b"\x14" * 20, "mpeg"),
    ("a.avi", b"RIFF\x00\x10\x00\x00AVI LIST" + b"\x00" * 20, "avi"),
    ("a.flv", b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00" * 20, "flv"),
    ("a.wmv", bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c") + b"\x00" * 20, "asf/wmv"),
    ("a.webm", b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01" + b"\x00" * 20, "matroska/webm"),
    ("a.mov", b"\x00\x00\x00\x08wide\x00\x00\x00\x20mdat" + b"\x00" * 20, "quicktime"),
    ("a.mov", b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00" * 20, "quicktime"),
    ("a.prores", b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00" * 20, "quicktime"),
    ("a.dnxhr", bytes.fromhex("060e2b34020501010d0102") + b"\x00" * 30, "mxf"),
])
def test_valid_headers_are_accepted(tmp_path, name, header, expected):
    p = tmp_path / name
    p.write_bytes(header + b"\x01" * 3000)
    assert cv.validate_video_content(str(p)) == expected


def test_renamed_container_is_accepted_with_a_warning(tmp_path):
    p = tmp_path / "actually_mp4.webm"
    p.write_bytes(MP4_HEADER + b"\x01" * 3000)
    notes = []
    assert cv.validate_video_content(str(p), warn=notes.append) == "mp4"
    assert any("looks like mp4" in n and ".webm" in n for n in notes)


def test_small_missing_and_non_files_are_rejected(tmp_path):
    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(MP4_HEADER)
    with pytest.raises(InvalidVideoFormat, match="too small"):
        cv.validate_video_content(str(tiny))
    with pytest.raises(InvalidVideoFormat, match="does not exist"):
        cv.validate_video_content(str(tmp_path / "nope.mp4"))
    folder = tmp_path / "dir.mp4"
    folder.mkdir()
    with pytest.raises(InvalidVideoFormat, match="Not a regular file"):
        cv.validate_video_content(str(folder))


@pytest.mark.skipif(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs a non-root POSIX user")
def test_unreadable_file_is_rejected_with_the_reason(tmp_path):
    p = tmp_path / "locked.mp4"
    p.write_bytes(MP4_HEADER + b"\x01" * 3000)
    p.chmod(0)
    try:
        with pytest.raises(InvalidVideoFormat, match="Cannot read"):
            cv.validate_video_content(str(p))
    finally:
        p.chmod(0o600)


def test_extension_filter_is_case_insensitive_and_exact():
    from youtube_upload import formats
    assert formats.is_supported_extension("A.MP4") and formats.is_supported_extension("x.y.webm")
    assert not formats.is_supported_extension("movie") and not formats.is_supported_extension("movie.mp4.txt")
    assert not formats.is_supported_extension("notes.mkv")
    with pytest.raises(InvalidVideoFormat, match="Accepted extensions"):
        formats.validate_video_format("a.txt")
