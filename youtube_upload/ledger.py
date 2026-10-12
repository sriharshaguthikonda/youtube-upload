"""Local record of what was uploaded to which channel (idempotency, #8).

Duplicate detection used ``search().list(forMine=True, q=...)``: fuzzy,
eventually consistent (a video uploaded minutes ago may not be found) and 100
quota units per call. The ledger is exact and free. It is keyed by *channel
id*, so two accounts never share state and re-authorising a label as a
different Google user cannot cause a false "already uploaded".
"""
import contextlib
import errno
import os
import re
import threading
import time
from pathlib import Path

from . import lib, paths

_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _file_lock(lock_path, timeout=10.0, sleep=time.sleep, clock=time.time):
    """Cross-process mutual exclusion for a read-modify-write of one state file.

    Built on :class:`ProcessLock` (``flock`` / ``msvcrt.locking``): nothing goes stale after a
    crash, and there is no create-if-absent race. The earlier "create ``X.lock`` exclusively"
    version raised ``PermissionError`` (not ``FileExistsError``) on Windows when several
    processes raced, which CI caught.
    """
    lock = ProcessLock(lock_path)
    deadline = clock() + timeout
    while not lock.acquire():
        if clock() > deadline:
            raise TimeoutError("Could not lock {0}; another upload may be running.".format(lock_path))
        sleep(0.05)
    try:
        yield
    finally:
        lock.release()


# errno values meaning "somebody else holds it" (POSIX flock / Windows msvcrt.locking).
_BUSY_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}
if hasattr(errno, "EDEADLOCK"):
    _BUSY_ERRNOS.add(errno.EDEADLOCK)


class ProcessLock(object):
    """Exclusive lock on a file, held by this process and dropped by the OS when it dies.

    Unlike "create a lock file" schemes there is nothing stale to clean up after a crash or
    ``kill -9``. Two ``ProcessLock`` objects on the same path exclude each other even inside one
    process (``flock`` locks belong to the open file description, ``msvcrt.locking`` to the handle).
    """

    def __init__(self, path):
        self.path = Path(path)
        self._handle = None
        self.degraded = False  # True: the file system cannot lock (some NFS/SMB mounts); not enforced

    def acquire(self):
        """Try to take the lock without waiting. ``True`` if now held.

        Only genuine contention returns ``False``. If the file system does not support locking
        at all (ENOLCK/ENOTSUP...), waiting would never end, so the lock degrades to a no-op
        and ``degraded`` says so.
        """
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(self.path), "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _BUSY_ERRNOS:
                handle.close()
                return False
            self.degraded = True   # cannot lock here: carry on unlocked rather than wait forever
        self._handle = handle
        return True

    def release(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # closing the handle releases it anyway
        finally:
            handle.close()


def upload_lock_name(channel_id, sha256):
    """File name of the in-flight lock for one (channel, file content) pair."""
    return "{0}-{1}.lock".format(_SAFE.sub("_", channel_id), sha256[:32])


def prune_old_locks(directory, max_age=7 * 24 * 3600, clock=time.time):
    """Delete lock files nobody has touched for ``max_age`` seconds (never a held one)."""
    directory = Path(directory)
    try:
        candidates = list(directory.glob("*.lock"))
    except OSError:
        return
    for path in candidates:
        try:
            if clock() - path.stat().st_mtime < max_age:
                continue
            probe = ProcessLock(path)
            if probe.acquire():          # unlinking a lock someone holds would break exclusion
                try:
                    probe.release()
                    path.unlink()
                except OSError:
                    pass
        except OSError:
            continue


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
