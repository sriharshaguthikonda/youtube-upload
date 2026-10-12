"""Shared fixtures: isolated per-user directories, fake clock, fake Google bits.

Nothing in the suite talks to the network or to a real Google account. Every
credential value used here is an obvious fake generated at test time.
"""
import json
import os
import sys
from pathlib import Path

import googleapiclient.errors
import httplib2
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point every per-user location at a temp dir so tests never touch real data."""
    home = tmp_path / "home"
    home.mkdir()
    app = tmp_path / "appdata"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(app))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(app))
    monkeypatch.setenv("YOUTUBE_UPLOAD_HOME", str(app / "youtube-upload"))
    monkeypatch.delenv("OAUTHLIB_RELAX_TOKEN_SCOPE", raising=False)
    monkeypatch.setenv("YOUTUBE_UPLOAD_NO_FFPROBE", "1")
    yield home


class FakeClock(object):
    """Deterministic replacement for time.sleep / time.monotonic."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


def make_http_error(status, reason=None, message="boom", headers=None):
    """Build a real googleapiclient HttpError like the API client raises."""
    info = {"status": str(status)}
    info.update(headers or {})
    body = {"error": {"code": status, "message": message}}
    if reason:
        body["error"]["errors"] = [{"reason": reason, "message": message}]
    return googleapiclient.errors.HttpError(httplib2.Response(info), json.dumps(body).encode("utf-8"))


@pytest.fixture
def http_error():
    return make_http_error


FAKE_CLIENT_ID = "fake-client-id.apps.googleusercontent.com"
FAKE_CLIENT_SECRET = "fake-client-secret-for-tests"


def write_client_secrets(path, client_id=FAKE_CLIENT_ID, client_secret=FAKE_CLIENT_SECRET):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}), encoding="utf-8")
    return path


@pytest.fixture
def client_secrets(tmp_path):
    return write_client_secrets(tmp_path / "secrets" / "client_secrets.json")


def mode_of(path):
    return os.stat(str(path)).st_mode & 0o777
