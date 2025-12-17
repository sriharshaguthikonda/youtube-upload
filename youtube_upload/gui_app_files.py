from pathlib import Path

import youtube_upload.main as cli_main


def filter_supported_video_paths(paths):
    """
    Split paths into supported and unsupported based on configured extensions.
    Returns (supported_list, unsupported_list).
    """
    supported = []
    unsupported = []
    for p in paths:
        suffix = Path(p).suffix.lower().lstrip(".")
        if suffix and suffix in cli_main.SUPPORTED_VIDEO_EXTENSIONS:
            supported.append(p)
        else:
            unsupported.append(p)
    return supported, unsupported
