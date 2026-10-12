"""Builders shared by the upload tests."""
import hashlib
from pathlib import Path

from youtube_upload import main

CHUNK = 256 * 1024
GUI_CHUNK = 8 * 1024 * 1024      # the GUI does not expose --chunksize
# 24-byte ftyp box: size, "ftyp", major brand, minor version, two compatible brands
MP4_HEADER = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


def write_mp4(path, size=3 * CHUNK + 1000, seed="a"):
    """Write a file with a valid MP4 signature and deterministic, seed-dependent filler."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    filler = hashlib.shake_256(seed.encode()).digest(size - len(MP4_HEADER))   # fast, any length
    path.write_bytes(MP4_HEADER + filler)
    return path


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_options(*argv, videos=("x.mp4",)):
    """Parsed + validated CLI options, exactly as the real CLI builds them."""
    parser = main.build_parser()
    options, _ = parser.parse_args(["--chunksize", str(CHUNK)] + list(argv))
    main.validate_options(options, list(videos), warn=lambda m: None)
    return options
