from . import formats


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
