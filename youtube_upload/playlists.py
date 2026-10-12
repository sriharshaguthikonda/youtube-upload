import sys

from . import retry
from .lib import debug


def _call(retrier, func, what):
    return (retrier or retry.Retrier()).call(func, what=what)


def get_playlist(youtube, title, retrier=None):
    """Return the user's playlist ID with this exact title (``None`` if absent)."""
    playlists = youtube.playlists()
    request = playlists.list(mine=True, part="id,snippet", maxResults=50)
    while request:
        results = _call(retrier, request.execute, "playlist listing")
        for item in results.get("items", []):
            if item.get("snippet", {}).get("title") == title:
                return item.get("id")
        request = playlists.list_next(request, results)
    return None


def create_playlist(youtube, title, privacy, retrier=None):
    """Create a playlist by title and return its ID.

    Creating is not idempotent: if an attempt failed in a way that leaves it unknown whether the
    server processed it (lost response, 5xx), look for the playlist before POSTing again.
    """
    debug("Creating playlist: {0}".format(title))
    attempts = {"n": 0}

    def insert():
        if attempts["n"]:
            existing = get_playlist(youtube, title)       # own retrier: counters stay separate
            if existing:
                return {"id": existing}
        attempts["n"] += 1
        return youtube.playlists().insert(part="snippet,status", body={
            "snippet": {"title": title},
            "status": {"privacyStatus": privacy},
        }).execute()
    response = _call(retrier, insert, "playlist creation")
    return response.get("id")


def video_in_playlist(youtube, playlist_id, video_id, retrier=None):
    """Return True if the video is already in the playlist."""
    playlist_items = youtube.playlistItems()
    request = playlist_items.list(
        part="id", playlistId=playlist_id, videoId=video_id, maxResults=1)
    while request:
        results = _call(retrier, request.execute, "playlist membership check")
        if results.get("items"):
            return True
        request = playlist_items.list_next(request, results)
    return False


def add_video_to_existing_playlist(youtube, playlist_id, video_id, retrier=None):
    """Add video to playlist (by identifier). Returns the response, or ``None`` if present."""
    if video_in_playlist(youtube, playlist_id, video_id, retrier):
        debug("Video already in playlist: {0}".format(video_id))
        return None
    debug("Adding video to playlist: {0}".format(playlist_id))
    attempts = {"n": 0}

    def insert():
        if attempts["n"] and video_in_playlist(youtube, playlist_id, video_id):
            return None                                   # an earlier attempt did go through
        attempts["n"] += 1
        return youtube.playlistItems().insert(part="snippet", body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        }).execute()
    return _call(retrier, insert, "playlist insert")


def confirm_create_on_terminal(title):
    """Ask on an interactive terminal; create silently when there is none (GUI, pipes)."""
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            answer = input('Playlist "{0}" not found. Create it? [y/N]: '.format(title)).strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False
    return True


def add_video_to_playlist(youtube, video_id, title, privacy="public", retrier=None,
                          confirm_create=confirm_create_on_terminal, cache=None):
    """Add video to a playlist chosen by title, creating it if the user agrees.

    ``cache`` (a dict) remembers title -> playlist id (or ``None`` when the user
    declined) so a batch looks the playlist up, and asks, only once.
    Returns the API response, ``None`` if nothing needed doing, and raises
    ``LookupError`` if the playlist is missing and creation was declined.
    """
    cache = cache if cache is not None else {}
    if title not in cache:
        playlist_id = get_playlist(youtube, title, retrier)
        if not playlist_id:
            if confirm_create(title):
                playlist_id = create_playlist(youtube, title, privacy, retrier)
        cache[title] = playlist_id
    playlist_id = cache[title]
    if not playlist_id:
        raise LookupError('Playlist "{0}" does not exist and was not created.'.format(title))
    return add_video_to_existing_playlist(youtube, playlist_id, video_id, retrier)
