"""Small helpers shared by the CLI and the library code."""
import re
import sys

_REDACTIONS = [
    # secrets that travel in a URL query or form body
    (re.compile(r"(?i)([?&;\s](?:upload_id|token|access_token|refresh_token|id_token|code|client_secret|"
                r"api_key|key|assertion)=)[^&\s'\"<>)]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=\-]+"), r"\1[redacted]"),
    # well-known token shapes, wherever they appear
    (re.compile(r"ya29\.[A-Za-z0-9_\-]{8,}"), "[redacted]"),
    (re.compile(r"1//[A-Za-z0-9_\-]{20,}"), "[redacted]"),
    (re.compile(r"GOCSPX-[A-Za-z0-9_\-]{8,}"), "[redacted]"),
]


def redact(text):
    """Blank out credentials, upload-session ids and authorization codes found in ``text``.

    Defence in depth: our own messages never include them, but exception texts from the HTTP
    stack print the full URL, and an upload session URL is a capability.
    """
    text = str(text)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def debug(obj, fd=None):
    """Write ``obj`` to standard error (a no-op when there is no stderr).

    ``pythonw.exe`` (the windowless GUI launcher) has ``sys.stderr = None``.
    """
    fd = fd if fd is not None else sys.stderr
    if fd is None:
        return
    obj = redact(obj)
    try:
        print(obj, file=fd)
    except (UnicodeEncodeError, OSError):
        # A console that cannot show the text must not abort an upload.
        try:
            print(str(obj).encode("ascii", "replace").decode("ascii"), file=fd)
        except Exception:
            pass


def catch_exceptions(exit_codes, fun, *args, **kwargs):
    """Run ``fun(*args, **kwargs)`` and return a process exit code.

    ``exit_codes`` maps exception classes to codes (or to callables taking the
    exception and returning a code). A subclass of a mapped class gets its
    parent's code (the previous exact-class lookup raised ``KeyError`` for
    subclasses). Returns 0 when nothing is raised.
    """
    try:
        fun(*args, **kwargs)
        return 0
    except tuple(exit_codes.keys()) as exc:
        debug("[{0}] {1}".format(exc.__class__.__name__, exc))
        for klass in type(exc).__mro__:
            if klass in exit_codes:
                code = exit_codes[klass]
                return code(exc) if callable(code) else code
        return 1  # unreachable: the except clause guarantees a match


def string_to_dict(string):
    """Return a dict from ``"key1=value1, key2=value2"`` (``None`` if empty).

    Raises ``ValueError`` with a readable message on malformed input.
    """
    if not string:
        return None
    result = {}
    for pair in (s.strip() for s in string.split(",")):
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError("expected key=value, got {0!r}".format(pair))
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("empty key in {0!r}".format(pair))
        result[key] = value.strip()
    return result or None


def sha256_file(path, cancel_check=None, chunk_size=4 * 1024 * 1024):
    """SHA-256 hex digest of a file, checking ``cancel_check()`` between chunks."""
    import hashlib
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            if cancel_check is not None and cancel_check():
                from .errors import UploadCancelled
                raise UploadCancelled("Cancelled by user")
            block = handle.read(chunk_size)
            if not block:
                return digest.hexdigest()
            digest.update(block)
