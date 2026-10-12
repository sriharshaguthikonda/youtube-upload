"""Video titles: derivation from file names, YouTube limits, honest warnings (#12)."""
import re
import unicodedata
from pathlib import Path

from .errors import OptionsError

MAX_TITLE_LENGTH = 100
FALLBACK_TITLE = "Untitled"
# YouTube rejects titles containing angle brackets ("invalidTitle").
_FORBIDDEN = "<>"


# Formatting characters that carry meaning (joiners in emoji sequences and Persian/Indic text,
# direction marks). Every other format character (BOM, zero-width space, soft hyphen) is junk.
_KEEP_FORMAT = {"\u200c", "\u200d", "\u200e", "\u200f"}
_DROP_CATEGORIES = {"Cc", "Cs", "Co", "Cn"}
_SPACE_CATEGORIES = {"Zs", "Zl", "Zp"}


def _clean_chars(text):
    """Return ``(cleaned, dropped_count)``.

    All kinds of space (no-break, ideographic, em) become a normal space; control characters
    and unassigned code points are dropped. ``str.isprintable()`` is not used: it is False for
    every non-ASCII space and joiner, which glued "Hello\u00a0World" into "HelloWorld".
    """
    out, dropped = [], 0
    for ch in text:
        category = unicodedata.category(ch)
        if ch in "\t\r\n\v\f" or category in _SPACE_CATEGORIES:
            out.append(" ")
        elif category in _DROP_CATEGORIES or (category == "Cf" and ch not in _KEEP_FORMAT):
            dropped += 1
        else:
            out.append(ch)
    return "".join(out), dropped


def _strip_unprintable(text):
    return _clean_chars(text)[0]


def _count_dropped(text):
    return _clean_chars(text)[1]


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


MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500


def clean_description(text, warn=None):
    """Make a description acceptable to the API: no angle brackets, at most 5000 bytes."""
    warn = warn or (lambda message: None)
    if text is None:
        return None
    cleaned = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch in "\n\r\t" or (category not in _DROP_CATEGORIES and (category != "Cf" or ch in _KEEP_FORMAT)):
            cleaned.append(ch)
    cleaned = "".join(cleaned)
    if "<" in cleaned or ">" in cleaned:
        # YouTube rejects them in descriptions; look-alike quotation marks keep the sense.
        cleaned = cleaned.replace("<", "\u2039").replace(">", "\u203a")
        warn("The description contained '<' or '>' which YouTube does not accept; "
             "they were replaced by \u2039 \u203a.")
    size = len(cleaned.encode("utf-8"))
    if size > MAX_DESCRIPTION_BYTES:
        raise OptionsError("The description is {0} bytes; YouTube allows at most {1} "
                           "(non-ASCII characters take 2-4 bytes each).".format(size, MAX_DESCRIPTION_BYTES))
    return cleaned


def clean_tags(text, warn=None):
    """Normalise a comma separated tag list; refuse one that exceeds YouTube's 500-character budget."""
    warn = warn or (lambda message: None)
    if not text:
        return text
    tags, changed = [], False
    for raw in text.split(","):
        tag = _strip_unprintable(raw).strip()
        if "<" in tag or ">" in tag:
            tag = tag.replace("<", "").replace(">", "").strip()
            changed = True
        if tag:
            tags.append(tag)
    if changed:
        warn("Angle brackets were removed from some tags (YouTube does not accept them).")
    # the API counts commas between tags and quotes around tags that contain spaces
    total = sum(len(t) + (2 if " " in t else 0) for t in tags) + max(len(tags) - 1, 0)
    if total > MAX_TAGS_CHARS:
        raise OptionsError("The tags add up to {0} characters; YouTube allows at most {1} in total "
                           "(commas and quotes around multi-word tags count).".format(total, MAX_TAGS_CHARS))
    return ", ".join(tags)
