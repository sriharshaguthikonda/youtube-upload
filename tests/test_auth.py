"""Stored credentials, refresh, migration, sign-out, multiple accounts (#3)."""
import json
import os
from datetime import datetime, timedelta

import pytest
import requests
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials

from tests.conftest import FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, mode_of, write_client_secrets
from youtube_upload import auth
from youtube_upload.errors import AuthenticationError, OptionsError, RequestError


def _naive_utc(delta):
    from datetime import timezone
    return (datetime.now(timezone.utc) + delta).replace(tzinfo=None)


def write_new_format(path, expiry_delta=timedelta(hours=-1), client_id=FAKE_CLIENT_ID,
                     refresh_token="fake-refresh-token", token="fake-old-access"):
    creds = Credentials(
        token=token, refresh_token=refresh_token, client_id=client_id,
        client_secret=FAKE_CLIENT_SECRET, token_uri=auth.DEFAULT_TOKEN_URI,
        scopes=list(auth.SCOPES), expiry=_naive_utc(expiry_delta))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    return path


def write_legacy_format(path, expired=True, with_expiry=True):
    """The JSON shape oauth2client used to write (all values are fake)."""
    info = {
        "_class": "OAuth2Credentials", "_module": "oauth2client.client",
        "access_token": "fake-legacy-access", "client_id": FAKE_CLIENT_ID,
        "client_secret": FAKE_CLIENT_SECRET, "refresh_token": "fake-legacy-refresh",
        "token_uri": "https://accounts.google.com/o/oauth2/token", "invalid": False,
        "revoke_uri": "https://accounts.google.com/o/oauth2/revoke", "user_agent": None,
        "scopes": list(auth.SCOPES) + ["https://www.googleapis.com/auth/youtube.readonly"],
        "token_expiry": (_naive_utc(timedelta(hours=-1) if expired else timedelta(hours=1))
                         .strftime("%Y-%m-%dT%H:%M:%SZ")) if with_expiry else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


@pytest.fixture
def fake_refresh(monkeypatch):
    calls = []

    def refresh(self, request):
        calls.append(self.refresh_token)
        self.token = "fake-new-access"
        self.expiry = _naive_utc(timedelta(hours=1))
    monkeypatch.setattr(Credentials, "refresh", refresh)
    return calls


# ------------------------------------------------------------ client secrets

def test_client_config_accepts_a_desktop_client(client_secrets):
    assert auth.load_client_config(client_secrets)["installed"]["client_id"] == FAKE_CLIENT_ID


def test_client_config_rejects_missing_file(tmp_path):
    with pytest.raises(OptionsError, match="not found"):
        auth.load_client_config(tmp_path / "nope.json")


def test_client_config_rejects_bad_json(tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(OptionsError, match="Cannot read"):
        auth.load_client_config(bad)


def test_client_config_explains_web_clients(tmp_path):
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8")
    with pytest.raises(OptionsError, match="Desktop app"):
        auth.load_client_config(web)


def test_client_config_rejects_the_shipped_example_placeholders():
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "examples" / "client_secrets.example.json"
    with pytest.raises(OptionsError, match="placeholder"):
        auth.load_client_config(example)


@pytest.mark.parametrize("drop", ["client_id", "client_secret", "token_uri", "auth_uri"])
def test_client_config_requires_keys(tmp_path, drop):
    path = write_client_secrets(tmp_path / "c.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["installed"][drop]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(OptionsError, match=drop):
        auth.load_client_config(path)


# -------------------------------------------------------- stored credentials

def test_not_signed_in_noninteractive_says_how_to_sign_in(tmp_path, client_secrets):
    with pytest.raises(AuthenticationError, match="--login"):
        auth.get_credentials(client_secrets, tmp_path / "none.json", interactive=False)


def test_valid_token_is_returned_without_refresh_or_rewrite(tmp_path, client_secrets, fake_refresh):
    creds_file = write_new_format(tmp_path / "a.json", expiry_delta=timedelta(hours=1))
    before = creds_file.read_bytes()
    creds = auth.get_credentials(client_secrets, creds_file, interactive=False)
    assert creds.token == "fake-old-access"
    assert fake_refresh == []
    assert creds_file.read_bytes() == before


def test_expired_token_is_refreshed_and_saved(tmp_path, client_secrets, fake_refresh):
    creds_file = write_new_format(tmp_path / "a.json")
    creds = auth.get_credentials(client_secrets, creds_file, interactive=False)
    assert fake_refresh == ["fake-refresh-token"]
    assert creds.token == "fake-new-access"
    assert json.loads(creds_file.read_text(encoding="utf-8"))["token"] == "fake-new-access"
    if os.name == "posix":
        assert mode_of(creds_file) == 0o600


def test_legacy_oauth2client_file_is_converted_in_place(tmp_path, client_secrets, fake_refresh):
    creds_file = write_legacy_format(tmp_path / "Electronics.json")
    notes = []
    creds = auth.get_credentials(client_secrets, creds_file, interactive=False, notify=notes.append)
    assert creds.refresh_token == "fake-legacy-refresh"
    assert fake_refresh == ["fake-legacy-refresh"]
    migrated = json.loads(creds_file.read_text(encoding="utf-8"))
    assert "_class" not in migrated and "access_token" not in migrated
    assert migrated["refresh_token"] == "fake-legacy-refresh" and migrated["token"] == "fake-new-access"
    assert any("old format" in n for n in notes)
    assert "fake-legacy-refresh" not in "\n".join(notes)  # migration never prints values
    # second run reads the new format directly
    notes.clear()
    auth.get_credentials(client_secrets, creds_file, interactive=False, notify=notes.append)
    assert not any("old format" in n for n in notes)


def test_legacy_file_without_known_expiry_is_forced_to_refresh(tmp_path, client_secrets, fake_refresh):
    creds_file = write_legacy_format(tmp_path / "a.json", with_expiry=False)
    auth.get_credentials(client_secrets, creds_file, interactive=False)
    assert fake_refresh == ["fake-legacy-refresh"]


def test_legacy_file_with_valid_token_is_still_rewritten_in_new_format(tmp_path, client_secrets, fake_refresh):
    creds_file = write_legacy_format(tmp_path / "a.json", expired=False)
    auth.get_credentials(client_secrets, creds_file, interactive=False)
    assert fake_refresh == []
    assert "token" in json.loads(creds_file.read_text(encoding="utf-8"))


@pytest.mark.parametrize("code,fragment", [
    ("invalid_grant", "expired"),
    ("invalid_client", "OAuth client"),
    ("unauthorized_client", "OAuth client"),
])
def test_refresh_rejection_gives_a_specific_message(tmp_path, client_secrets, monkeypatch, code, fragment):
    creds_file = write_new_format(tmp_path / "a.json")

    def refresh(self, request):
        raise RefreshError("{0}: Bad Request".format(code), {"error": code})
    monkeypatch.setattr(Credentials, "refresh", refresh)
    with pytest.raises(AuthenticationError) as info:
        auth.get_credentials(client_secrets, creds_file, interactive=False)
    assert fragment in str(info.value) and "--login" in str(info.value)


def test_interactive_mode_signs_in_again_after_a_revoked_token(tmp_path, client_secrets, monkeypatch):
    creds_file = write_new_format(tmp_path / "a.json")

    def refresh(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"})
    monkeypatch.setattr(Credentials, "refresh", refresh)
    fresh = Credentials("brand-new", refresh_token="r2", client_id=FAKE_CLIENT_ID,
                        client_secret=FAKE_CLIENT_SECRET, token_uri=auth.DEFAULT_TOKEN_URI,
                        scopes=list(auth.SCOPES), expiry=_naive_utc(timedelta(hours=1)))
    seen = {}

    def fake_login(secrets, creds, **kw):
        seen["args"] = (str(secrets), str(creds))
        return fresh
    monkeypatch.setattr(auth, "login", fake_login)
    notes = []
    result = auth.get_credentials(client_secrets, creds_file, interactive=True, notify=notes.append)
    assert result is fresh
    assert seen["args"][1] == str(creds_file)
    assert any("Starting the Google sign-in" in n for n in notes)


def test_changed_client_id_forces_new_sign_in(tmp_path, client_secrets, fake_refresh):
    creds_file = write_new_format(tmp_path / "a.json", client_id="some-old-rotated-client")
    with pytest.raises(AuthenticationError, match="different OAuth client"):
        auth.get_credentials(client_secrets, creds_file, interactive=False)
    assert fake_refresh == []  # never sent the old client's token to the new client


def test_network_failure_while_refreshing_is_not_reported_as_a_bad_login(tmp_path, client_secrets, monkeypatch):
    creds_file = write_new_format(tmp_path / "a.json")

    def refresh(self, request):
        raise TransportError("connection reset")
    monkeypatch.setattr(Credentials, "refresh", refresh)
    with pytest.raises(RequestError, match="network"):
        auth.get_credentials(client_secrets, creds_file, interactive=False)
    assert creds_file.exists()  # saved sign-in is kept


def test_corrupt_credentials_file_is_explained_and_replaced_when_interactive(tmp_path, client_secrets, monkeypatch):
    creds_file = tmp_path / "a.json"
    creds_file.write_text("{ not json", encoding="utf-8")
    with pytest.raises(AuthenticationError, match="not usable"):
        auth.get_credentials(client_secrets, creds_file, interactive=False)
    assert creds_file.read_text(encoding="utf-8") == "{ not json"  # not destroyed
    monkeypatch.setattr(auth, "login", lambda *a, **k: "LOGGED-IN")
    assert auth.get_credentials(client_secrets, creds_file, interactive=True, notify=lambda m: None) == "LOGGED-IN"


def test_refresh_token_missing_means_sign_in_again(tmp_path, client_secrets):
    creds_file = write_new_format(tmp_path / "a.json", refresh_token=None)
    with pytest.raises(AuthenticationError):  # from_authorized_user_info requires a refresh token
        auth.get_credentials(client_secrets, creds_file, interactive=False)


def test_save_failure_after_refresh_warns_but_keeps_working(tmp_path, client_secrets, fake_refresh, monkeypatch):
    creds_file = write_new_format(tmp_path / "a.json")

    def broken_save(credentials, path):
        raise OSError("disk full")
    monkeypatch.setattr(auth, "save_credentials", broken_save)
    notes = []
    creds = auth.get_credentials(client_secrets, creds_file, interactive=False, notify=notes.append)
    assert creds.token == "fake-new-access"
    assert any("could not save" in n and "disk full" in n for n in notes)


# ------------------------------------------------------------------ resource

def test_get_resource_builds_the_youtube_v3_service(tmp_path, client_secrets, fake_refresh):
    creds_file = write_new_format(tmp_path / "a.json")
    youtube = auth.get_resource(client_secrets, creds_file, interactive=False)
    assert callable(youtube.videos) and callable(youtube.playlistItems)


# ------------------------------------------------------------- multi-account

def test_two_accounts_keep_separate_credentials_and_do_not_interfere(tmp_path, client_secrets, fake_refresh):
    a = write_new_format(tmp_path / "accts" / "work.json", refresh_token="refresh-work")
    b = write_new_format(tmp_path / "accts" / "home.json", refresh_token="refresh-home")
    ca = auth.get_credentials(client_secrets, a, interactive=False)
    cb = auth.get_credentials(client_secrets, b, interactive=False)
    assert (ca.refresh_token, cb.refresh_token) == ("refresh-work", "refresh-home")
    assert json.loads(a.read_text(encoding="utf-8"))["refresh_token"] == "refresh-work"
    assert json.loads(b.read_text(encoding="utf-8"))["refresh_token"] == "refresh-home"


def test_list_accounts_reports_status_and_hides_secrets(tmp_path):
    accounts = tmp_path / "accts"
    write_new_format(accounts / "work.json")
    (accounts / "broken.json").write_text("nope", encoding="utf-8")
    write_client_secrets(accounts / "client_secrets.json")
    (accounts / "gui_settings.json").write_text("{}", encoding="utf-8")
    (accounts / "notes.txt").write_text("x", encoding="utf-8")
    found = auth.list_accounts(accounts)
    assert found == [
        {"label": "broken", "path": str(accounts / "broken.json"), "status": "unreadable"},
        {"label": "work", "path": str(accounts / "work.json"), "status": "signed-in"},
    ]
    assert "fake-refresh-token" not in json.dumps(found)


def test_list_accounts_of_missing_dir_is_empty(tmp_path):
    assert auth.list_accounts(tmp_path / "nope") == []


# ------------------------------------------------------------------- logout

class _Resp(object):
    def __init__(self, status):
        self.status_code = status


def test_logout_revokes_and_deletes(tmp_path):
    creds_file = write_new_format(tmp_path / "a.json")
    seen = {}

    def post(url, data=None, **kw):
        seen["url"], seen["token"] = url, data["token"]
        return _Resp(200)
    result = auth.logout(creds_file, http_post=post)
    assert (result.removed, result.revoked) == (True, True)
    assert seen["url"] == auth.REVOKE_URI and seen["token"] == "fake-refresh-token"
    assert not creds_file.exists()


def test_logout_deletes_locally_even_if_google_unreachable_and_says_so(tmp_path):
    creds_file = write_new_format(tmp_path / "a.json")

    def post(*a, **k):
        raise requests.ConnectionError("offline")
    result = auth.logout(creds_file, http_post=post)
    assert result.removed and not result.revoked
    assert "Could not reach Google" in result.message and "myaccount.google.com/permissions" in result.message
    assert not creds_file.exists()
    assert "fake-refresh-token" not in result.message


def test_logout_treats_already_invalid_token_as_revoked(tmp_path):
    creds_file = write_new_format(tmp_path / "a.json")
    result = auth.logout(creds_file, http_post=lambda *a, **k: _Resp(400))
    assert result.removed and result.revoked


def test_logout_without_revoke_does_not_call_google(tmp_path):
    creds_file = write_new_format(tmp_path / "a.json")

    def post(*a, **k):
        raise AssertionError("must not be called")
    result = auth.logout(creds_file, revoke=False, http_post=post)
    assert result.removed and not result.revoked and not creds_file.exists()


def test_logout_when_already_signed_out(tmp_path):
    result = auth.logout(tmp_path / "none.json", http_post=lambda *a, **k: _Resp(200))
    assert result.removed is False and "Already signed out" in result.message


def test_logout_removes_unreadable_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("garbage", encoding="utf-8")
    result = auth.logout(bad, http_post=lambda *a, **k: _Resp(200))
    assert result.removed and not bad.exists()


def test_logout_only_touches_the_chosen_account(tmp_path):
    a = write_new_format(tmp_path / "work.json")
    b = write_new_format(tmp_path / "home.json")
    auth.logout(a, http_post=lambda *a_, **k: _Resp(200))
    assert not a.exists() and b.exists()
