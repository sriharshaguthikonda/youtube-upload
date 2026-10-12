"""Video titles: derivation from file names, YouTube limits, honest warnings (#12)."""
import re
from pathlib import Path

from .errors import OptionsError

MAX_TITLE_LENGTH = 100
FALLBACK_TITLE = "Untitled"
# YouTube rejects titles containing angle brackets ("invalidTitle").
_FORBIDDEN = "<>"


def _strip_unprintable(text):
    """Whitespace controls become spaces; other control characters are dropped."""
    text = re.sub(r"[\t\r\n\v\f]", " ", text)
    return "".join(ch for ch in text if ch.isprintable())


def _count_dropped(text):
    text = re.sub(r"[\t\r\n\v\f]", " ", text)
    return sum(1 for ch in text if not ch.isprintable())


def sanitize_title(title, warn=None):
    """Turn a file-name-like string into a YouTube-safe title.

    Underscores become spaces (they are separators in file names). Hyphens are
    kept: YouTube allows them and they are often meaningful (dates, "A-B").
    ``warn(message)`` is called whenever anything *material* changes: removed
    characters, truncation, or the ``Untitled`` fallback.
    """
    warn = warn or (lambda message: None)
    original = title
    removed = _count_dropped(title) > 0
    cleaned = _strip_unprintable(title)
    cleaned = cleaned.replace("_", " ")
    for ch in _FORBIDDEN:
        if ch in cleaned:
            cleaned = cleaned.replace(ch, " ")
            removed = True
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > MAX_TITLE_LENGTH:
        warn("Title truncated to {0} characters: {1!r}".format(MAX_TITLE_LENGTH, original))
        cleaned = cleaned[:MAX_TITLE_LENGTH].rstrip()
    elif removed:
        warn("Unsupported characters were removed from the title {0!r}; using {1!r}".format(original, cleaned))
    if not cleaned:
        warn("Title {0!r} is empty after cleaning; falling back to {1!r}. Several files with "
             "no usable name will get the same title.".format(original, FALLBACK_TITLE))
        return FALLBACK_TITLE
    return cleaned


def default_title_from_video_path(video_path, warn=None):
    """A safe title derived from the file name (without extension)."""
    return sanitize_title(Path(video_path).stem, warn=warn)


def explicit_title(title, warn=None):
    """Validate a title the user typed: keep it as written except for hard limits."""
    warn = warn or (lambda message: None)
    cleaned = _strip_unprintable(title)
    for ch in _FORBIDDEN:
        cleaned = cleaned.replace(ch, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise OptionsError("The title is empty after removing unsupported characters.")
    if cleaned != title.strip():
        warn("Unsupported characters were removed from the title; using {0!r}".format(cleaned))
    if len(cleaned) > MAX_TITLE_LENGTH:
        warn("Title truncated to {0} characters.".format(MAX_TITLE_LENGTH))
        cleaned = cleaned[:MAX_TITLE_LENGTH].rstrip()
    return cleaned
