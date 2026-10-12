"""Which file extensions the uploader accepts (the list YouTube documents)."""
from pathlib import Path

from .errors import InvalidVideoFormat

SUPPORTED_VIDEO_EXTENSIONS = {
    "mov", "mpeg", "mpeg1", "mpeg2", "mpeg4", "mp4", "mpg", "avi", "wmv",
    "mpegps", "flv", "3gpp", "3gp", "webm", "dnxhr", "prores", "cineform",
    "hevc", "h265",
}


def is_supported_extension(path):
    return Path(path).suffix.lower().lstrip(".") in SUPPORTED_VIDEO_EXTENSIONS


def validate_video_format(video_path):
    """Raise :class:`InvalidVideoFormat` if the extension is not supported."""
    if not is_supported_extension(video_path):
        raise InvalidVideoFormat(
            "Unsupported video format for '{0}'. Accepted extensions: {1}".format(
                video_path, ", ".join(sorted(SUPPORTED_VIDEO_EXTENSIONS))))
