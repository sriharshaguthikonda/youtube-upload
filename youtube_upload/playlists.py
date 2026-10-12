import locale
import sys

from .lib import debug

def get_playlist(youtube, title):
    """Return users's playlist ID by title (None if not found)"""
    playlists = youtube.playlists()
    request = playlists.list(mine=True, part="id,snippet")
    current_encoding = locale.getpreferredencoding()
    
    while request:
        results = request.execute()
        for item in results["items"]:
            t = item.get("snippet", {}).get("title")
            existing_playlist_title = (t.encode(current_encoding) if hasattr(t, 'decode') else t)
            if existing_playlist_title == title:
                return item.get("id")
        request = playlists.list_next(request, results)

def create_playlist(youtube, title, privacy):
    """Create a playlist by title and return its ID"""
    debug("Creating playlist: {0}".format(title))
    response = youtube.playlists().insert(part="snippet,status", body={
        "snippet": {
            "title": title,
        },
        "status": {
            "privacyStatus": privacy,
        }
    }).execute()
    return response.get("id")

def video_in_playlist(youtube, playlist_id, video_id):
    """Return True if the video is already in the playlist."""
    playlist_items = youtube.playlistItems()
    request = playlist_items.list(
        part="id",
        playlistId=playlist_id,
        videoId=video_id,
        maxResults=1,
    )
    while request:
        results = request.execute()
        if results.get("items"):
            return True
        request = playlist_items.list_next(request, results)
    return False

def add_video_to_existing_playlist(youtube, playlist_id, video_id):
    """Add video to playlist (by identifier) and return the playlist ID."""
    if video_in_playlist(youtube, playlist_id, video_id):
        debug("Video already in playlist: {0}".format(video_id))
        return None
    debug("Adding video to playlist: {0}".format(playlist_id))
    return youtube.playlistItems().insert(part="snippet", body={
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            }
        }
    }).execute()
    
def add_video_to_playlist(youtube, video_id, title, privacy="public"):
    """Add video to playlist (by title) and return the full response."""
    playlist_id = get_playlist(youtube, title)
    if not playlist_id:
        # Ask user for confirmation when running interactively; otherwise create silently.
        create = True
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                answer = input(f'Playlist "{title}" not found. Create it? [y/N]: ').strip().lower()
                create = answer in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                create = False
        if create:
            playlist_id = create_playlist(youtube, title, privacy)
        else:
            debug(f'Skipping playlist creation for "{title}".')
            return None
    if playlist_id:
        return add_video_to_existing_playlist(youtube, playlist_id, video_id)
    else:
        debug("Error adding video to playlist")
