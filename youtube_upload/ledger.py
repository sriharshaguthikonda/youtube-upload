"""Local record of what was uploaded to which channel (idempotency, #8).

Duplicate detection used ``search().list(forMine=True, q=...)``: fuzzy,
eventually consistent (a video uploaded minutes ago may not be found) and 100
quota units per call. The ledger is exact and free. It is keyed by *channel
id*, so two accounts never share state and re-authorising a label as a
different Google user cannot cause a false "already uploaded".
"""
import contextlib
import os
import re
import threading
import time
from pathlib import Path

from . import lib, paths

_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _file_lock(lock_path, timeout=10.0, stale_after=60.0, sleep=time.sleep, clock=time.time):
    """Cross-process lock via an exclusively created file (works on Windows too)."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = clock() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if clock() - lock_path.stat().st_mtime > stale_after:
                    lock_path.unlink()  # a crashed process left it behind
                    continue
            except OSError:
                pass
            if clock() > deadline:
                raise TimeoutError("Could not lock {0}; another upload may be running.".format(lock_path))
            sleep(0.05)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


class Ledger(object):
    """``sha256 -> {video_id, title, size, name, uploaded_at}`` for one channel."""

    def __init__(self, directory, channel_id, warn=lib.debug, clock=time.time):
        self._path = Path(directory) / "{0}.json".format(_SAFE.sub("_", channel_id))
        self._warn = warn
        self._clock = clock

    @property
    def path(self):
        return self._path

    def _read(self):
        return paths.read_json_or_quarantine(self._path, warn=self._warn).get("videos", {})

    def get(self, sha256):
        entry = self._read().get(sha256)
        return entry if isinstance(entry, dict) and entry.get("video_id") else None

    def record(self, sha256, video_id, title, size, name):
        with _THREAD_LOCK, _file_lock(self._path.with_suffix(".lock")):
            videos = self._read()  # re-read under the lock: never lose another process's entry
            videos[sha256] = {
                "video_id": video_id, "title": title, "size": size, "name": name,
                "uploaded_at": int(self._clock()),
            }
            paths.write_private_json(self._path, {"version": 1, "videos": videos})

    def forget(self, sha256):
        with _THREAD_LOCK, _file_lock(self._path.with_suffix(".lock")):
            videos = self._read()
            if videos.pop(sha256, None) is not None:
                paths.write_private_json(self._path, {"version": 1, "videos": videos})
