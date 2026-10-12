"""A fake YouTube Data API *HTTP server* to plug under the real google client.

``googleapiclient.discovery.build("youtube", "v3", http=FakeYouTubeServer())``
builds the genuine client from the bundled discovery document, so request URLs,
parameters and the resumable-upload protocol are exercised for real; only the
network is replaced. Faults (HTTP errors, connection resets, a killed process)
can be injected deterministically. No Google account or network is involved.
"""
import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse

import googleapiclient.discovery
import httplib2


class SimulatedCrash(BaseException):
    """The process dies (power cut, kill -9): not caught by ``except Exception``."""


def _response(status, headers=None):
    info = {"status": str(status)}
    info.update(headers or {})
    return httplib2.Response(info)


def json_response(status, payload, headers=None):
    h = {"content-type": "application/json; charset=UTF-8"}
    h.update(headers or {})
    return _response(status, h), json.dumps(payload).encode("utf-8")


def error_response(status, reason=None, message="error", headers=None):
    body = {"error": {"code": status, "message": message}}
    if reason:
        body["error"]["errors"] = [{"domain": "youtube", "reason": reason, "message": message}]
    return json_response(status, body, headers)


class Fault(object):
    def __init__(self, method, path_part, outcome, times=1, after_bytes=None, after_commit=False):
        self.method, self.path_part, self.outcome = method, path_part, outcome
        self.times = times
        self.after_bytes = after_bytes
        self.after_commit = after_commit   # process the request first, then fail (response lost)


class FakeYouTubeServer(object):
    def __init__(self, channel_id="UCfakechannel00000000001", channel_title="Fake Channel"):
        self.channel_id = channel_id
        self.channel_title = channel_title
        self.uploads_playlist = "UU" + channel_id[2:]
        self.log = []                      # (method, path, query)
        self.sessions = {}                 # session id -> dict
        self.videos = {}                   # video id -> resource dict
        self.upload_order = []             # video ids newest first (uploads playlist order)
        self.playlists = {}                # playlist id -> {"title":..., "items": [video ids]}
        self.faults = []
        self.bytes_put = 0                 # payload bytes received over all PUTs
        self.sessions_started = 0
        self.page_size = 50
        self.drop_tags = False             # simulate YouTube discarding the tag
        self.final_upload_status = "uploaded"
        self.omit_id_in_final_response = False
        self._n = 0

    # -- building the client --------------------------------------------------
    def client(self):
        return googleapiclient.discovery.build(
            "youtube", "v3", http=self, static_discovery=True, cache_discovery=False)

    # -- fault injection ------------------------------------------------------
    def inject(self, method, path_part, status=None, reason=None, exc=None, times=1,
               headers=None, after_bytes=None, after_commit=False):
        """Fail the next ``times`` matching requests with an HTTP error or exception.

        ``after_bytes``: only once the upload session holds at least that many bytes.
        ``after_commit``: let the server process the request, then fail (lost response).
        """
        if exc is not None:
            outcome = exc
        else:
            outcome = error_response(status, reason, headers=headers)
        self.faults.append(Fault(method, path_part, outcome, times, after_bytes, after_commit))

    def requests_to(self, method, path_part):
        return [r for r in self.log if r[0] == method and path_part in r[1]]

    @property
    def search_calls(self):
        return len(self.requests_to("GET", "/search"))

    def add_existing_video(self, title, tags=None, description="", channel_id=None, video_id=None):
        self._n += 1
        video_id = video_id or "oldVid%05d" % self._n
        self.videos[video_id] = {
            "kind": "youtube#video", "id": video_id,
            "snippet": {"title": title, "description": description, "tags": tags or [],
                        "channelId": channel_id or self.channel_id},
            "status": {"uploadStatus": "processed", "privacyStatus": "public"},
        }
        self.upload_order.insert(0, video_id)
        return video_id

    # -- httplib2.Http interface -----------------------------------------------
    def request(self, uri, method="GET", body=None, headers=None, redirections=5, connection_type=None):
        parsed = urlparse(uri)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.log.append((method, path, query))

        for fault in list(self.faults):
            if fault.method != method or fault.path_part not in path:
                continue
            session = self.sessions.get(path.rsplit("/", 1)[-1]) if method == "PUT" else None
            committed = session["committed"] if session else 0
            if fault.after_bytes is not None and not fault.after_commit and committed < fault.after_bytes:
                continue
            if fault.after_commit:
                response = self._route(method, path, query, body, headers)
                committed = session["committed"] if session else 0
                if fault.after_bytes is not None and committed < fault.after_bytes:
                    return response
            fault.times -= 1
            if fault.times <= 0:
                self.faults.remove(fault)
            outcome = fault.outcome
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return self._route(method, path, query, body, headers)

    # -- the fake API -----------------------------------------------------------
    def _route(self, method, path, query, body, headers):
        if method == "POST" and path.endswith("/upload/youtube/v3/videos"):
            return self._start_session(query, body)
        if method == "PUT" and "/session/" in path:
            return self._put(path, body, headers)
        if method == "GET" and path.endswith("/youtube/v3/channels"):
            return json_response(200, {"items": [{
                "id": self.channel_id, "snippet": {"title": self.channel_title},
                "contentDetails": {"relatedPlaylists": {"uploads": self.uploads_playlist}}}]})
        if method == "GET" and path.endswith("/youtube/v3/videos"):
            ids = (query.get("id") or "").split(",")
            return json_response(200, {"items": [self.videos[i] for i in ids if i in self.videos]})
        if method == "GET" and path.endswith("/youtube/v3/playlistItems"):
            return self._list_playlist_items(query)
        if method == "POST" and path.endswith("/youtube/v3/playlistItems"):
            data = json.loads(body)["snippet"]
            self.playlists[data["playlistId"]]["items"].append(data["resourceId"]["videoId"])
            return json_response(200, {"id": "item%d" % len(self.log), "snippet": data})
        if method == "GET" and path.endswith("/youtube/v3/playlists"):
            items = [{"id": pid, "snippet": {"title": p["title"]}} for pid, p in self.playlists.items()]
            return json_response(200, {"items": items})
        if method == "POST" and path.endswith("/youtube/v3/playlists"):
            data = json.loads(body)
            pid = "PL%020d" % (len(self.playlists) + 1)
            self.playlists[pid] = {"title": data["snippet"]["title"], "items": []}
            return json_response(200, {"id": pid, "snippet": data["snippet"]})
        if "/thumbnails/set" in path:
            return json_response(200, {"items": [{"default": {"url": "https://example.invalid/t.jpg"}}]})
        if method == "GET" and path.endswith("/youtube/v3/search"):
            return json_response(200, {"items": []})
        return error_response(404, "notFound", "fake server has no route for %s %s" % (method, path))

    def _list_playlist_items(self, query):
        if query.get("playlistId") in self.playlists:
            ids = self.playlists[query["playlistId"]]["items"]
            items = [{"id": "i" + v, "snippet": {}, "contentDetails": {"videoId": v}}
                     for v in ids if not query.get("videoId") or v == query["videoId"]]
            return json_response(200, {"items": items})
        if query.get("playlistId") != self.uploads_playlist:
            return error_response(404, "playlistNotFound")
        start = int(query.get("pageToken") or 0)
        window = self.upload_order[start:start + self.page_size]
        items = []
        for vid in window:
            video = self.videos[vid]
            items.append({"snippet": {"title": video["snippet"]["title"],
                                      "description": video["snippet"].get("description", "")},
                          "contentDetails": {"videoId": vid}})
        payload = {"items": items}
        if start + self.page_size < len(self.upload_order):
            payload["nextPageToken"] = str(start + self.page_size)
        return json_response(200, payload)

    def _start_session(self, query, body):
        self.sessions_started += 1
        sid = "s%d" % self.sessions_started
        meta = json.loads(body) if body else {}
        self.sessions[sid] = {"committed": 0, "size": None, "meta": meta, "sha": hashlib.sha256()}
        return _response(200, {"location": "https://upload.example.invalid/session/" + sid}), b""

    def _put(self, path, body, headers):
        sid = path.rsplit("/", 1)[-1]
        session = self.sessions.get(sid)
        if session is None:
            return error_response(404, "notFound", "Upload session not found")
        crange = headers.get("content-range", "")
        status_query = re.match(r"bytes \*/(\d+)$", crange)
        if status_query:
            size = int(status_query.group(1))
            return self._progress_response(session, size)
        match = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)$", crange)
        if not match:
            return error_response(400, "badRequest", "bad Content-Range %r" % crange)
        start, end, size = int(match.group(1)), int(match.group(2)), match.group(3)
        if start != session["committed"]:
            return error_response(400, "badRequest", "chunk starts at %d, server has %d" % (start, session["committed"]))
        data = body.read() if hasattr(body, "read") else (body or b"")
        if len(data) != end - start + 1:
            return error_response(400, "badRequest", "body has %d bytes, Content-Range says %d" % (
                len(data), end - start + 1))
        session["sha"].update(data)
        self.bytes_put += len(data)
        session["committed"] = end + 1
        if size != "*":
            session["size"] = int(size)
        response = self._progress_response(session, session["size"])
        return response

    def _progress_response(self, session, size):
        if size is not None and session["committed"] >= size:
            return self._complete(session)
        headers = {}
        if session["committed"] > 0:
            headers["range"] = "bytes=0-%d" % (session["committed"] - 1)
        return _response(308, headers), b""

    def _complete(self, session):
        if "video_id" not in session:
            self._n += 1
            video_id = "fakeVid%04d" % self._n
            session["video_id"] = video_id
            snippet = dict(session["meta"].get("snippet", {}))
            if self.drop_tags:
                snippet["tags"] = []
            snippet["channelId"] = self.channel_id
            self.uploaded_sha256 = getattr(self, "uploaded_sha256", {})
            self.uploaded_sha256[video_id] = session["sha"].hexdigest()   # what the server really received
            self.videos[video_id] = {
                "kind": "youtube#video", "id": video_id, "snippet": snippet,
                "status": dict(session["meta"].get("status", {}),
                               uploadStatus=self.final_upload_status),
            }
            self.upload_order.insert(0, video_id)
        resource = dict(self.videos[session["video_id"]])
        if self.omit_id_in_final_response:
            resource.pop("id")
        return json_response(200, resource)
