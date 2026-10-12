"""Where per-user data lives, and safe ways to read and write it.

Nothing here is stored inside the source tree: tokens and state go under the
per-user application-data directory so they cannot be committed by accident.
"""
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

from .errors import OptionsError

ENV_HOME = "YOUTUBE_UPLOAD_HOME"

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    "com%d" % i for i in range(1, 10)
} | {"lpt%d" % i for i in range(1, 10)}
# Unicode word characters, dots, dashes and inner spaces. No separators, no
# leading dot, no trailing dot or space (Windows silently strips those).
_LABEL_RE = re.compile(r"^\w(?:[\w.\- ]{0,62}[\w\-])?$")


def app_home():
    """Return the per-user data directory (not created)."""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "youtube-upload"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "youtube-upload"


def legacy_accounts_dir():
    """Default accounts directory used by earlier versions."""
    return Path.home() / ".youtube-upload-accounts"


def default_accounts_dir():
    """Accounts directory when ``--account`` is used without ``--accounts-dir``.

    An existing legacy directory keeps being used so saved sign-ins are found.
    """
    legacy = legacy_accounts_dir()
    if legacy.is_dir():
        return legacy
    return app_home() / "accounts"


def state_dir():
    """Directory for the upload ledger and resumable-session records."""
    return app_home() / "state"


def validate_account_label(label):
    """Return ``label`` if it is safe to use as a file name, else raise."""
    if label is None or not isinstance(label, str):
        raise OptionsError("Account label must be text.")
    stripped = label.strip()
    if (
        not stripped
        or stripped != label
        or not _LABEL_RE.match(stripped)
        or stripped.split(".")[0].lower() in _WINDOWS_RESERVED
    ):
        raise OptionsError(
            "Invalid account label {0!r}: use letters, digits, '_', '-', '.' and inner "
            "spaces only (max 64 characters, no path separators).".format(label)
        )
    return stripped


def short_hash(text, length=12):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _replace_with_retries(src, dst, attempts=6, base_delay=0.05):
    """``os.replace`` that tolerates Windows briefly locking the target.

    Antivirus scanners and search indexers often hold a freshly written file for a few
    milliseconds, which makes the rename fail with ``PermissionError`` on Windows.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(base_delay * (attempt + 1))


def write_private_text(path, text):
    """Atomically write ``text`` to ``path``; owner-only permissions on POSIX.

    The data is written to a temporary file in the same directory (created
    ``0600`` by ``mkstemp``), flushed to disk, and then renamed into place, so
    a crash or full disk can never leave a half-written token file behind.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retries(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_private_json(path, data):
    write_private_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json_or_quarantine(path, warn=None):
    """Return the parsed JSON dict at ``path``; ``{}`` if missing.

    A file that is not valid JSON is moved aside to ``<name>.corrupt-<time>``
    (never deleted, never silently overwritten) and ``{}`` is returned, so one
    corrupt state file cannot wedge every future run.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data
    except ValueError as exc:
        aside = path.with_name("%s.corrupt-%d" % (path.name, int(time.time())))
        try:
            os.replace(str(path), str(aside))
            moved = "moved to %s" % aside
        except OSError as move_exc:
            moved = "could not be moved aside (%s)" % move_exc
        if warn:
            warn("State file %s is corrupt (%s) and was %s; starting fresh." % (path, exc, moved))
        return {}
