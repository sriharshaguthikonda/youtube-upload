"""Resumable upload of one video file.

Contract with ``google-api-python-client`` (verified against the installed
library in tests/test_upload_video.py, which drives the *real*
``HttpRequest.next_chunk`` with a scripted HTTP transport):

* ``videos().insert(..., media_body=MediaFileUpload(resumable=True))`` returns
  an ``HttpRequest``. The first ``next_chunk()`` POSTs the metadata and the
  response ``Location`` header becomes ``request.resumable_uri``; every later
  ``next_chunk()`` PUTs one chunk starting at ``request.resumable_progress``.
* After a failed chunk the library marks the request as being in an error
  state, and the next ``next_chunk()`` first asks the server how many bytes it
  has (``PUT`` with ``Content-Range: bytes */<size>``). So *within* a process,
  retrying ``next_chunk()`` is the correct way to resume.
* To resume in a *new* process we do the same query ourselves through the
  public ``request.http`` and then set the public ``resumable_uri`` and
  ``resumable_progress`` attributes. No private attribute is touched.

The session URI is a capability for that upload, so it is stored with
owner-only permissions and never logged.
"""
import hashlib
import json
import re
import time
from pathlib import Path

import googleapiclient.errors
import googleapiclient.http

from . import lib, paths, retry
from .errors import RequestError, UploadCancelled

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Google keeps a resumable session for about a week; stay well inside that.
SESSION_MAX_AGE = 5 * 24 * 3600
MAX_STALLED_CHUNKS = 50   # consecutive "308" replies that do not advance the offset
MIN_CHUNK = 256 * 1024


def extract_video_id(response):
    """Return the video id from an insert response, or raise.

    A "successful" upload with no usable id must never be reported as success
    (and must never silently trigger a second upload): the caller is told to
    look in YouTube Studio first.
    """
    video_id = response.get("id") if isinstance(response, dict) else None
    kind = response.get("kind") if isinstance(response, dict) else None
    if not isinstance(video_id, str) or not VIDEO_ID_RE.match(video_id) or kind not in (None, "youtube#video"):
        raise RequestError(
            "YouTube finished the upload but did not return a valid video id. Check YouTube "
            "Studio before uploading again, to avoid a duplicate.")
    return video_id


def body_digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def session_key(channel_id, file_sha256, size):
    return hashlib.sha256("{0}|{1}|{2}".format(channel_id, file_sha256, size).encode("utf-8")).hexdigest()


class SessionStore(object):
    """On-disk record of the resumable session of an unfinished upload."""

    def __init__(self, directory, clock=time.time, warn=lib.debug):
        self.directory = Path(directory)
        self._clock = clock
        self._warn = warn

    def _file(self, key):
        return self.directory / "{0}.json".format(key)

    def load(self, key, size, digest):
        """Return the saved session URI if it still matches this upload."""
        data = paths.read_json_or_quarantine(self._file(key), warn=self._warn)
        if not data:
            return None
        fresh = isinstance(data.get("created"), (int, float)) and self._clock() - data["created"] < SESSION_MAX_AGE
        if data.get("size") != size or data.get("digest") != digest or not fresh or not data.get("uri"):
            self.discard(key)  # a different file, different metadata, or too old
            return None
        return data["uri"]

    def save(self, key, uri, size, digest):
        """Persist the session. Returns False (after a warning) instead of raising.

        Resumability is a convenience; a full disk or an antivirus lock on the state folder
        must never fail an upload (nor discard one that has just completed).
        """
        try:
            paths.write_private_json(self._file(key), {
                "uri": uri, "size": size, "digest": digest, "created": self._clock()})
            return True
        except OSError as exc:
            self._warn("Could not save the resumable-upload record ({0}); this upload cannot be "
                       "resumed if interrupted.".format(exc))
            return False

    def discard(self, key):
        try:
            self._file(key).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._warn("Could not remove resumable-session record: {0}".format(exc))


class UploadSession(object):
    """A :class:`SessionStore` bound to one upload (key, size, metadata)."""

    def __init__(self, store, key):
        self.store = store
        self.key = key

    def load(self, size, body):
        return self.store.load(self.key, size, body_digest(body))

    def save(self, uri, size, body):
        return self.store.save(self.key, uri, size, body_digest(body))

    def discard(self):
        self.store.discard(self.key)


class _SessionRecordingHttp(object):
    """Wraps ``request.http`` to save the session URI the moment Google issues it.

    ``next_chunk()`` creates the session and sends the first chunk in one call, so a record
    written only after it returns leaves a window (the whole file of a small video!) in which a
    crash loses the session although the server may already hold every byte.
    """

    def __init__(self, inner, on_session):
        self._inner = inner
        self._on_session = on_session

    def request(self, uri, method="GET", body=None, headers=None, *args, **kwargs):
        resp, content = self._inner.request(uri, method, body=body, headers=headers, *args, **kwargs)
        if method == "POST" and getattr(resp, "status", None) == 200 and resp.get("location"):
            self._on_session(resp["location"])
        return resp, content

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _drop_none(value):
    """Remove ``None`` values (recursively) so unset options are not sent as null."""
    if isinstance(value, dict):
        cleaned = {k: _drop_none(v) for k, v in value.items() if v is not None}
        return cleaned
    return value


def _query_status(request, uri, size, retrier):
    """Ask Google how much of the session ``uri`` it has: (kind, value)."""
    headers = {"Content-Range": "bytes */{0}".format(size), "content-length": "0"}

    def ask():
        resp, content = request.http.request(uri, "PUT", headers=headers)
        if resp.status not in (200, 201, 308, 404, 410):
            # httplib2 returns 5xx/429 as a response instead of raising. Raise here so the
            # retrier sees it: a transient error must not make us discard a good session.
            raise googleapiclient.errors.HttpError(resp, content, uri=uri)
        return resp, content

    resp, content = retrier.call(ask, what="resumable status query")
    status = resp.status
    if status in (200, 201):
        return "complete", request.postproc(resp, content)
    if status == 308:
        rng = resp.get("range") if hasattr(resp, "get") else None
        try:
            return "partial", (int(rng.split("-")[1]) + 1 if rng else 0)
        except (ValueError, IndexError):
            return "partial", 0
    return "gone", None   # 404 / 410: the session no longer exists


def upload(resource, path, body, chunksize=8 * 1024 * 1024, progress_callback=None,
           retrier=None, session=None, log=lib.debug):
    """Upload ``path`` with ``body`` as metadata. Returns the new video id.

    ``retrier`` supplies bounded retries and cancellation (see :mod:`retry`).
    ``session`` (an :class:`UploadSession`) enables resuming after the process
    is killed or the network is lost for longer than the retry budget.
    """
    retrier = retrier or retry.Retrier(log=log)
    if chunksize < MIN_CHUNK or chunksize % MIN_CHUNK:
        raise ValueError("chunksize must be a positive multiple of {0} bytes".format(MIN_CHUNK))
    body = _drop_none(body)
    part = ",".join(body.keys())
    media = googleapiclient.http.MediaFileUpload(
        str(path), chunksize=chunksize, resumable=True, mimetype="application/octet-stream")
    size = media.size()

    persisted = {"uri": None}

    def remember(uri):
        if session and session.save(uri, size, body):
            persisted["uri"] = uri

    def new_request():
        fresh = resource.videos().insert(part=part, body=body, media_body=media)
        fresh.http = _SessionRecordingHttp(fresh.http, remember)
        return fresh

    request = new_request()

    saved_uri = session.load(size, body) if session else None
    if saved_uri:
        try:
            kind, value = _query_status(request, saved_uri, size, retrier)
        except googleapiclient.errors.HttpError as exc:
            failure = retry.classify_exception(exc)
            if failure.kind in (retry.AUTH, retry.QUOTA):
                raise  # signing in again / waiting is needed; a fresh upload would fail too
            log("Saved upload session was rejected ({0}); starting a fresh upload.".format(failure.message))
            kind = "gone"
        if kind == "complete":
            session.discard()
            log("The interrupted upload had actually finished on YouTube; using its result.")
            return extract_video_id(value)
        if kind == "partial":
            request.resumable_uri = saved_uri
            request.resumable_progress = value
            persisted["uri"] = saved_uri
            log("Resuming the interrupted upload at {0:.1f}%.".format(100.0 * value / size if size else 100))
            if progress_callback:
                progress_callback(size, value)
        else:
            session.discard()
            log("The saved upload session is no longer valid; starting a fresh upload.")

    restarts = 0
    stalled = 0
    last_progress = request.resumable_progress
    while True:
        try:
            status, response = retrier.call(request.next_chunk, what="upload chunk")
        except googleapiclient.errors.HttpError as exc:
            failure = retry.classify_exception(exc)
            if failure.kind == retry.NOT_FOUND and request.resumable_uri and restarts < 1:
                # Session expired server-side mid-upload: begin again, once.
                restarts += 1
                log("Upload session expired ({0}); restarting the upload.".format(failure.message))
                if session:
                    session.discard()
                    persisted["uri"] = None
                request = new_request()
                stalled, last_progress = 0, request.resumable_progress
                continue
            raise
        if response is not None:
            break                      # finished: nothing below may turn this into a failure
        if session and request.resumable_uri and request.resumable_uri != persisted["uri"]:
            remember(request.resumable_uri)
        if request.resumable_progress > last_progress:
            stalled, last_progress = 0, request.resumable_progress
        else:
            stalled += 1
            if stalled >= MAX_STALLED_CHUNKS:
                raise RequestError(
                    "YouTube is not making progress on the upload ({0} consecutive replies without "
                    "advancing past byte {1}).".format(stalled, last_progress))
        if status is not None and progress_callback:
            progress_callback(status.total_size, status.resumable_progress)

    video_id = extract_video_id(response)
    if session:
        session.discard()
    if progress_callback:
        try:
            progress_callback(size, size)
        except UploadCancelled:
            # Cancel arrived while the last chunk was on the wire. The video exists now; the
            # caller records it and honours the cancel before the NEXT file.
            log("Cancel requested, but this upload had already completed.")
    return video_id


def verify_video(resource, video_id, retrier=None):
    """Confirm YouTube lists ``video_id`` and has not rejected it. Returns the item."""
    retrier = retrier or retry.Retrier()
    response = retrier.call(
        lambda: resource.videos().list(part="status,snippet", id=video_id).execute(),
        what="video verification")
    items = response.get("items") or []
    if not items or items[0].get("id") != video_id:
        raise RequestError("YouTube does not list video {0} after the upload.".format(video_id))
    status = items[0].get("status") or {}
    state = status.get("uploadStatus")
    if state in ("failed", "rejected", "deleted"):
        detail = status.get("failureReason") or status.get("rejectionReason") or "no reason given"
        raise RequestError("YouTube reports video {0} as {1}: {2}.".format(video_id, state, detail))
    return items[0]
