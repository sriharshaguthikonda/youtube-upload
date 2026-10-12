"""Channel lookup and the "does this already exist on the channel?" index (#8).

Quota costs (default daily quota is 10,000 units): ``search.list`` is 100
units per call, ``playlistItems.list`` and ``videos.list`` are 1 unit per call
(up to 50 results). The index below walks the channel's *uploads playlist*,
which is exact and immediately consistent, once per account per run.
"""
import re
from collections import namedtuple

from . import retry
from .errors import AuthenticationError

Channel = namedtuple("Channel", "id title uploads_playlist")

HASH_PREFIX = "hash:"
# uploadStatus values meaning YouTube did not keep the video: it must not count as a duplicate.
BAD_UPLOAD_STATUSES = {"failed", "rejected", "deleted"}
_HASH_RE = re.compile(r"hash:([0-9a-f]{64})")


def hash_tag(sha256):
    return HASH_PREFIX + sha256


def get_channel(youtube, retrier=None):
    """Return the authenticated user's :class:`Channel`."""
    retrier = retrier or retry.Retrier()
    response = retrier.call(
        lambda: youtube.channels().list(part="id,snippet,contentDetails", mine=True).execute(),
        what="channel lookup")
    items = response.get("items") or []
    if not items:
        raise AuthenticationError(
            "This Google account has no YouTube channel. Create one at https://www.youtube.com/ "
            "(or sign in with the account that owns the channel) and try again.")
    item = items[0]
    uploads = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    if not item.get("id") or not uploads:
        raise AuthenticationError("YouTube returned an incomplete channel record; try signing in again.")
    return Channel(item["id"], (item.get("snippet") or {}).get("title", ""), uploads)


class RemoteIndex(object):
    """Titles and content hashes of the newest videos on a channel."""

    def __init__(self, youtube, channel, retrier=None, max_videos=1000, log=None):
        self._youtube = youtube
        self._channel = channel
        self._retrier = retrier or retry.Retrier()
        self._max = max_videos
        self._log = log or (lambda message: None)
        self._by_title = None
        self._by_hash = None
        self.truncated = False

    def _scan(self):
        by_title, by_hash, ids, bad_ids = {}, {}, [], set()
        token = None
        while True:
            def page(token=token):
                return self._youtube.playlistItems().list(
                    part="snippet,contentDetails", playlistId=self._channel.uploads_playlist,
                    maxResults=50, pageToken=token).execute()
            response = self._retrier.call(page, what="uploads listing")
            for item in response.get("items") or []:
                video_id = (item.get("contentDetails") or {}).get("videoId") \
                    or ((item.get("snippet") or {}).get("resourceId") or {}).get("videoId")
                snippet = item.get("snippet") or {}
                title = snippet.get("title") or ""
                if not video_id or title in ("Deleted video", "Private video"):
                    continue
                ids.append(video_id)
                by_title.setdefault(title.strip().lower(), video_id)
                match = _HASH_RE.search(snippet.get("description") or "")
                if match:
                    by_hash.setdefault(match.group(1), video_id)
            token = response.get("nextPageToken")
            if not token:
                break
            if len(ids) >= self._max:
                self.truncated = True  # more pages exist than we are willing to read
                break
        # Tags are not in playlist items: read them in batches of 50 (1 unit each).
        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            response = self._retrier.call(
                lambda batch=batch: self._youtube.videos().list(
                    part="snippet,status", id=",".join(batch)).execute(),  # maxResults is not allowed with id
                what="video tag listing")
            for item in response.get("items") or []:
                if (item.get("status") or {}).get("uploadStatus") in BAD_UPLOAD_STATUSES:
                    bad_ids.add(item["id"])
                    continue
                for tag in (item.get("snippet") or {}).get("tags") or []:
                    if tag.startswith(HASH_PREFIX) and len(tag) == len(HASH_PREFIX) + 64:
                        by_hash.setdefault(tag[len(HASH_PREFIX):], item["id"])
        if bad_ids:   # videos YouTube failed/rejected are not duplicates
            by_title = {t: v for t, v in by_title.items() if v not in bad_ids}
            by_hash = {h: v for h, v in by_hash.items() if v not in bad_ids}
        if self.truncated:
            self._log("Channel has more than {0} videos; only the newest {0} were checked "
                      "for duplicates.".format(self._max))
        self._by_title, self._by_hash = by_title, by_hash

    def _ensure(self):
        if self._by_title is None:
            self._scan()

    def find_by_hash(self, sha256):
        self._ensure()
        return self._by_hash.get(sha256)

    def find_by_title(self, title):
        self._ensure()
        return self._by_title.get(title.strip().lower())

    def add(self, video_id, title, sha256=None):
        """Remember a video uploaded in this run (keeps later checks exact)."""
        if self._by_title is None:
            return
        self._by_title.setdefault(title.strip().lower(), video_id)
        if sha256:
            self._by_hash.setdefault(sha256, video_id)
