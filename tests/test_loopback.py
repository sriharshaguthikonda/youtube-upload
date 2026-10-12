"""OAuth loopback flow: real local server, fake Google (#3)."""
import http.client
import json
import os
import threading
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
import requests
from google.oauth2.credentials import Credentials

from tests.conftest import FAKE_CLIENT_ID, mode_of, write_client_secrets
from youtube_upload import auth
from youtube_upload.auth.loopback import run_loopback_flow
from youtube_upload.errors import AuthenticationError, UploadCancelled


class FakeFlow(object):
    def __init__(self, granted=None, refresh_token="fake-refresh", fetch_error=None):
        self.redirect_uri = None
        self.state = None
        self.auth_kwargs = None
        self.fetched_code = None
        self._fetch_error = fetch_error
        scopes = list(auth.SCOPES if granted is None else granted)
        self.oauth2session = SimpleNamespace(token={"scope": scopes})
        self._credentials = Credentials("fake-access", refresh_token=refresh_token,
                                        client_id="x", client_secret="y",
                                        token_uri="https://oauth2.example/token",
                                        scopes=list(auth.SCOPES))

    def authorization_url(self, **kwargs):
        self.auth_kwargs = kwargs
        self.state = kwargs["state"]
        return "https://accounts.example/auth?state=" + self.state, self.state

    def fetch_token(self, code=None, **kwargs):
        if self._fetch_error:
            raise self._fetch_error
        self.fetched_code = code

    @property
    def credentials(self):
        return self._credentials


def _get(flow, query, path="/"):
    host, port = "127.0.0.1", int(flow.redirect_uri.rsplit(":", 1)[1].strip("/"))
    conn = http.client.HTTPConnection(host, port, timeout=5)  # bypasses any env proxy
    try:
        conn.request("GET", path + ("?" + urlencode(query) if query else ""))
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def _browser(flow, *requests_to_make):
    """A fake browser: performs the given requests from a thread."""
    def opener(url):
        def go():
            for query, path in requests_to_make:
                query = {k: (flow.state if v is None else v) for k, v in query.items()}
                try:
                    _get(flow, query, path)
                except OSError:
                    pass
        threading.Thread(target=go, daemon=True).start()
        return True
    return opener


def _run(flow, opener, **kw):
    notes = []
    kw.setdefault("timeout", 5)
    kw.setdefault("poll", 0.02)
    creds = run_loopback_flow(flow, auth.SCOPES, notify=notes.append, browser_opener=opener, **kw)
    return creds, notes


def test_successful_sign_in_exchanges_code_with_pkce_state_and_offline_consent():
    flow = FakeFlow()
    creds, notes = _run(flow, _browser(flow, ({"state": None, "code": "abc123"}, "/")))
    assert flow.fetched_code == "abc123"
    assert creds.refresh_token == "fake-refresh"
    assert flow.redirect_uri.startswith("http://127.0.0.1:")
    assert flow.auth_kwargs["access_type"] == "offline"
    assert flow.auth_kwargs["prompt"] == "consent"  # guarantees a refresh token
    assert any("accounts.example/auth" in n for n in notes)  # URL is shown for manual use


def test_stray_and_forged_requests_are_ignored_until_the_real_redirect():
    flow = FakeFlow()
    opener = _browser(
        flow,
        ({}, "/favicon.ico"),
        ({"state": "forged", "code": "evil"}, "/"),
        ({"code": "evil-no-state"}, "/"),
        ({"state": None, "code": "good"}, "/"),
    )
    _run(flow, opener)
    assert flow.fetched_code == "good"


def test_denied_consent_is_a_clear_error():
    flow = FakeFlow()
    with pytest.raises(AuthenticationError, match="cancelled or access was denied"):
        _run(flow, _browser(flow, ({"state": None, "error": "access_denied"}, "/")))
    assert flow.fetched_code is None


def test_timeout_when_nobody_signs_in():
    flow = FakeFlow()
    with pytest.raises(AuthenticationError, match="Timed out"):
        _run(flow, lambda url: False, timeout=0.3)


def test_cancel_stops_waiting():
    flow = FakeFlow()
    ticks = {"n": 0}

    def cancel():
        ticks["n"] += 1
        return ticks["n"] > 3

    with pytest.raises(UploadCancelled):
        _run(flow, lambda url: False, cancel=cancel)


def test_missing_permission_is_reported_not_saved():
    flow = FakeFlow(granted=["https://www.googleapis.com/auth/youtube.upload"])
    with pytest.raises(AuthenticationError, match="missing.*auth/youtube"):
        _run(flow, _browser(flow, ({"state": None, "code": "c"}, "/")))


def test_no_refresh_token_is_reported():
    flow = FakeFlow(refresh_token=None)
    with pytest.raises(AuthenticationError, match="no refresh token"):
        _run(flow, _browser(flow, ({"state": None, "code": "c"}, "/")))


def test_token_endpoint_failure_is_wrapped():
    flow = FakeFlow(fetch_error=ValueError("invalid_grant: Bad Request"))
    with pytest.raises(AuthenticationError, match="rejected the sign-in code"):
        _run(flow, _browser(flow, ({"state": None, "code": "c"}, "/")))


def test_authorization_code_never_reaches_the_logs(capfd):
    flow = FakeFlow()
    _run(flow, _browser(flow, ({"state": None, "code": "SUPER-SECRET-CODE-123"}, "/")))
    captured = capfd.readouterr()
    assert "SUPER-SECRET-CODE-123" not in captured.out + captured.err


def test_no_browser_available_still_prints_the_url():
    flow = FakeFlow()

    def broken_opener(url):
        raise RuntimeError("no display")

    ticks = {"n": 0}

    def cancel():
        ticks["n"] += 1
        return ticks["n"] > 2

    notes = []
    with pytest.raises(UploadCancelled):
        run_loopback_flow(flow, auth.SCOPES, notify=notes.append, cancel=cancel,
                          timeout=5, poll=0.02, browser_opener=broken_opener)
    text = "\n".join(notes)
    assert "no display" in text and "accounts.example/auth" in text


# ---- the real google-auth-oauthlib flow, with only the token endpoint faked ----

def test_end_to_end_login_with_real_flow_stores_private_token_file(tmp_path, monkeypatch):
    secrets = write_client_secrets(tmp_path / "client_secrets.json")
    creds_file = tmp_path / "accounts" / "work.json"
    posted = {}

    def fake_request(self, method, url, **kwargs):
        posted.update(url=url, data=kwargs.get("data"))
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({
            "access_token": "fake-access", "refresh_token": "fake-refresh",
            "expires_in": 3600, "token_type": "Bearer",
            "scope": " ".join(auth.SCOPES),
        }).encode("utf-8")
        response.encoding = "utf-8"
        response.url = url
        response.request = requests.Request("POST", url).prepare()
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)

    def opener(url):
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(url).query)
        redirect = query["redirect_uri"][0]
        assert query["client_id"] == [FAKE_CLIENT_ID]
        assert query["code_challenge_method"] == ["S256"]  # PKCE
        assert query["access_type"] == ["offline"]
        port = int(redirect.rsplit(":", 1)[1].strip("/"))

        def go():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/?" + urlencode({"state": query["state"][0], "code": "the-code"}))
            conn.getresponse().read()
            conn.close()
        threading.Thread(target=go, daemon=True).start()
        return True

    from youtube_upload.auth import loopback
    real_run = loopback.run_loopback_flow
    monkeypatch.setattr(auth, "run_loopback_flow",
                        lambda flow, scopes, **kw: real_run(flow, scopes, browser_opener=opener, poll=0.02, **kw))

    creds = auth.login(str(secrets), str(creds_file), timeout=5)

    assert creds.refresh_token == "fake-refresh"
    assert posted["data"]["code"] == "the-code"
    assert posted["data"]["code_verifier"]  # PKCE verifier sent to the token endpoint
    assert posted["data"]["redirect_uri"].startswith("http://127.0.0.1:")
    stored = json.loads(creds_file.read_text(encoding="utf-8"))
    assert stored["refresh_token"] == "fake-refresh" and stored["client_id"] == FAKE_CLIENT_ID
    if os.name == "posix":
        assert mode_of(creds_file) == 0o600


def test_fixed_port_is_used_for_the_redirect():
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    flow = FakeFlow()
    run_loopback_flow(flow, auth.SCOPES, notify=lambda m: None, timeout=5, poll=0.02, port=port,
                      browser_opener=_browser(flow, ({"state": None, "code": "abc"}, "/")))
    assert flow.redirect_uri == "http://127.0.0.1:%d/" % port and flow.fetched_code == "abc"


def test_port_in_use_is_a_clear_error():
    import socket
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    try:
        port = held.getsockname()[1]
        with pytest.raises(AuthenticationError, match="Cannot listen on 127.0.0.1:%d" % port):
            run_loopback_flow(FakeFlow(), auth.SCOPES, notify=lambda m: None, timeout=1, port=port)
    finally:
        held.close()
