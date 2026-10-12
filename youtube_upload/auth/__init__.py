"""Google OAuth 2.0 for installed apps: sign in, refresh, sign out, many accounts.

Replaces the ``oauth2client`` out-of-band flow, which Google disabled in 2022
and which could not complete a first-time sign-in at all. Sign-in now uses the
loopback redirect (``http://127.0.0.1:<port>/``) with PKCE through
``google-auth-oauthlib``; tokens are refreshed through ``google-auth``.

One credentials file per account. Files written by the old ``oauth2client``
based versions are read transparently and rewritten in the new format on first
use (the secret values never leave the file).
"""
import json
import os
from collections import namedtuple
from datetime import datetime
from pathlib import Path

import google_auth_httplib2
import googleapiclient.discovery
import googleapiclient.http
import requests
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .. import lib, paths
from ..errors import AuthenticationError, OptionsError, RequestError
from .loopback import run_loopback_flow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
YOUTUBE_UPLOAD_SCOPE = SCOPES  # name used by earlier versions
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
PERMISSIONS_URL = "https://myaccount.google.com/permissions"
DEFAULT_HTTP_TIMEOUT = 300

_PLACEHOLDER_PREFIX = "REPLACE"
_NON_ACCOUNT_FILES = {"client_secrets.json", "gui_settings.json"}


# ---------------------------------------------------------------------------
# Client secrets (the OAuth *application*, not the user)
# ---------------------------------------------------------------------------

def load_client_config(client_secrets_file):
    """Validate and return the parsed client-secrets JSON ("installed" app)."""
    path = Path(client_secrets_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise OptionsError("Client secrets file not found: {0}".format(path)) from None
    except (OSError, ValueError) as exc:
        raise OptionsError("Cannot read client secrets file {0}: {1}".format(path, exc)) from exc
    if not isinstance(data, dict):
        raise OptionsError("{0} is not a Google OAuth client file.".format(path))
    if "web" in data and "installed" not in data:
        raise OptionsError(
            "{0} is a 'Web application' OAuth client. Create a 'Desktop app' client in "
            "Google Cloud Console (APIs & Services > Credentials) and download that one.".format(path))
    config = data.get("installed")
    if not isinstance(config, dict):
        raise OptionsError("{0} has no \"installed\" section: not a Desktop-app OAuth client "
                           "file from Google Cloud Console.".format(path))
    for key in ("client_id", "client_secret", "auth_uri", "token_uri"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise OptionsError("{0} is missing \"{1}\".".format(path, key))
    if config["client_id"].startswith(_PLACEHOLDER_PREFIX) or config["client_secret"].startswith(_PLACEHOLDER_PREFIX):
        raise OptionsError("{0} still contains the example placeholder values; download your "
                           "own client file from Google Cloud Console.".format(path))
    return data


# ---------------------------------------------------------------------------
# Stored user credentials
# ---------------------------------------------------------------------------

def _is_legacy_format(info):
    return info.get("_class") == "OAuth2Credentials" or ("access_token" in info and "token" not in info)


def _parse_legacy_expiry(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")  # naive UTC, as google-auth expects
    except (TypeError, ValueError):
        return None


def _from_legacy(info):
    """Convert an ``oauth2client`` credentials dict to google-auth credentials."""
    refresh_token = info.get("refresh_token")
    if not refresh_token:
        raise AuthenticationError("The saved sign-in has no refresh token.")
    expiry = _parse_legacy_expiry(info.get("token_expiry"))
    scopes = info.get("scopes")
    if isinstance(scopes, str):
        scopes = scopes.split()
    return Credentials(
        # Without a known expiry, force a refresh instead of trusting the old token.
        token=info.get("access_token") if expiry else None,
        refresh_token=refresh_token,
        token_uri=info.get("token_uri") or DEFAULT_TOKEN_URI,
        client_id=info.get("client_id"),
        client_secret=info.get("client_secret"),
        scopes=scopes or None,
        expiry=expiry,
    )


def load_stored_credentials(credentials_file):
    """Return ``(credentials, was_legacy)``; ``(None, False)`` if not signed in.

    Raises :class:`AuthenticationError` if the file exists but is unusable.
    """
    path = Path(credentials_file)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, False
    except OSError as exc:
        raise AuthenticationError("Cannot read the saved sign-in {0}: {1}".format(path, exc)) from exc
    try:
        info = json.loads(text)
        if not isinstance(info, dict):
            raise ValueError("expected a JSON object")
        if _is_legacy_format(info):
            return _from_legacy(info), True
        return Credentials.from_authorized_user_info(info), False
    except AuthenticationError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthenticationError(
            "The saved sign-in file {0} is not usable ({1}). Sign in again to replace it.".format(
                path, exc)) from exc


def save_credentials(credentials, credentials_file):
    """Atomically store ``credentials`` (owner-only permissions on POSIX)."""
    paths.write_private_text(credentials_file, credentials.to_json())


def credentials_status(credentials_file):
    """``"signed-in"``, ``"signed-out"`` or ``"unreadable"``; no network, no secrets."""
    try:
        creds, _ = load_stored_credentials(credentials_file)
    except AuthenticationError:
        return "unreadable"
    return "signed-in" if creds is not None and creds.refresh_token else "signed-out"


def _refresh_error_code(exc):
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, dict) and arg.get("error"):
            return str(arg["error"])
    text = str(exc)
    for code in ("invalid_grant", "invalid_client", "unauthorized_client", "access_denied"):
        if code in text:
            return code
    return None


def describe_refresh_error(exc, client_secrets_file=""):
    code = _refresh_error_code(exc)
    if code == "invalid_grant":
        return ("Google no longer accepts the saved sign-in (it expired, was revoked, or the "
                "password or permissions changed). Apps whose OAuth consent screen is in "
                "'Testing' status lose sign-in after 7 days.")
    if code in ("invalid_client", "unauthorized_client"):
        return ("Google does not accept the OAuth client in {0} (deleted, disabled, or its secret "
                "was rotated). Download the current client file and sign in again.".format(
                    client_secrets_file or "client_secrets.json"))
    return "Google refused to refresh the saved sign-in: {0}".format(exc)


# ---------------------------------------------------------------------------
# Sign in / get usable credentials / sign out
# ---------------------------------------------------------------------------

def login(client_secrets_file, credentials_file, open_browser=True, notify=None,
          cancel=None, timeout=300, port=0):
    """Run the browser sign-in and store the result. Returns the credentials."""
    config = load_client_config(client_secrets_file)
    # PKCE is requested explicitly: older google-auth-oauthlib releases do not enable it by default.
    flow = InstalledAppFlow.from_client_config(config, SCOPES, autogenerate_code_verifier=True)
    credentials = run_loopback_flow(
        flow, SCOPES, open_browser=open_browser, notify=notify, cancel=cancel, timeout=timeout,
        port=port)
    try:
        save_credentials(credentials, credentials_file)
    except OSError as exc:
        raise AuthenticationError(
            "Signed in, but the sign-in could not be saved to {0}: {1}".format(credentials_file, exc)) from exc
    return credentials


def get_credentials(client_secrets_file, credentials_file, interactive=True,
                    open_browser=True, notify=None, cancel=None, timeout=300, port=0):
    """Return valid credentials, refreshing or signing in as needed.

    With ``interactive=False`` any situation that would need the browser raises
    :class:`AuthenticationError` with instructions instead.
    """
    notify = notify or lib.debug
    config = load_client_config(client_secrets_file)
    client_id = config["installed"]["client_id"]
    problem = None

    try:
        credentials, was_legacy = load_stored_credentials(credentials_file)
    except AuthenticationError as exc:
        credentials, was_legacy, problem = None, False, str(exc)

    if credentials is not None and credentials.client_id and credentials.client_id != client_id:
        credentials = None
        problem = ("The saved sign-in was made with a different OAuth client than "
                   "{0} (was the client replaced or rotated?).".format(client_secrets_file))

    if credentials is not None:
        if was_legacy:
            notify("Converting the saved sign-in from the old format.")
        try:
            refreshed = False
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise RefreshError("invalid_grant", {"error": "invalid_grant"})
                credentials.refresh(Request())
                refreshed = True
            if was_legacy or refreshed:
                try:
                    save_credentials(credentials, credentials_file)
                except OSError as exc:
                    notify("Warning: could not save the refreshed sign-in to {0}: {1}".format(
                        credentials_file, exc))
            return credentials
        except TransportError as exc:
            raise RequestError(
                "Could not reach Google to refresh the sign-in (network problem): {0}".format(exc)) from exc
        except RefreshError as exc:
            problem = describe_refresh_error(exc, client_secrets_file)

    if problem is None:
        problem = "Not signed in yet."
    if not interactive:
        raise AuthenticationError(
            "{0} Sign in with:  youtube-upload --login {1}".format(
                problem, "--credentials-file {0}".format(credentials_file)))
    notify("{0} Starting the Google sign-in.".format(problem))
    return login(client_secrets_file, credentials_file, open_browser=open_browser,
                 notify=notify, cancel=cancel, timeout=timeout, port=port)


def get_resource(client_secrets_file, credentials_file, interactive=True,
                 open_browser=True, notify=None, cancel=None, timeout=300, port=0):
    """Authenticate and return a ``googleapiclient`` YouTube v3 resource."""
    credentials = get_credentials(
        client_secrets_file, credentials_file, interactive=interactive,
        open_browser=open_browser, notify=notify, cancel=cancel, timeout=timeout, port=port)
    # google-auth-httplib2 refreshes the access token itself during long uploads. build_http()
    # already excludes HTTP 308 (resume-incomplete) from redirect handling; its 60 s per-request
    # timeout is also a bound on sending one whole chunk, which a link below ~1 Mbit/s cannot
    # meet for an 8 MiB chunk, so the timeout is raised (YOUTUBE_UPLOAD_HTTP_TIMEOUT overrides).
    http = googleapiclient.http.build_http()
    http.timeout = http_timeout()
    return googleapiclient.discovery.build(
        "youtube", "v3", http=google_auth_httplib2.AuthorizedHttp(credentials, http=http),
        cache_discovery=False)


def http_timeout():
    try:
        value = int(os.environ.get("YOUTUBE_UPLOAD_HTTP_TIMEOUT", DEFAULT_HTTP_TIMEOUT))
    except ValueError:
        value = DEFAULT_HTTP_TIMEOUT
    return min(max(value, 30), 3600)


LogoutResult = namedtuple("LogoutResult", "removed revoked message")


def logout(credentials_file, revoke=True, http_post=requests.post):
    """Sign out: revoke the token at Google (best effort) and delete the file.

    The local file is always deleted when it exists. If Google could not be
    reached the result says so, because the token then stays valid until it is
    removed at https://myaccount.google.com/permissions.
    """
    path = Path(credentials_file)
    try:
        credentials, _ = load_stored_credentials(path)
    except AuthenticationError:
        credentials = None  # unreadable file: still remove it below
    if not path.exists():
        return LogoutResult(False, False, "Already signed out.")

    revoked, notes = False, []
    token = getattr(credentials, "refresh_token", None) or getattr(credentials, "token", None)
    if revoke and token:
        try:
            # The token goes in the body, never the URL: exception texts print the URL.
            response = http_post(
                REVOKE_URI, data={"token": token}, timeout=15,
                headers={"content-type": "application/x-www-form-urlencoded"})
            status = getattr(response, "status_code", None)
            if status == 200:
                revoked = True
            elif status == 400:
                revoked = True
                notes.append("Google reported the token was already invalid.")
            else:
                notes.append("Google answered HTTP {0} to the revoke request.".format(status))
        except requests.RequestException as exc:
            notes.append("Could not reach Google to revoke the token ({0}).".format(type(exc).__name__))
    elif revoke:
        notes.append("There was no usable token to revoke.")

    try:
        path.unlink()
    except OSError as exc:
        return LogoutResult(False, revoked, "Could not delete {0}: {1}".format(path, exc))
    if revoke and not revoked:
        notes.append("Remove the app at {0} to be sure access is cut off.".format(PERMISSIONS_URL))
    return LogoutResult(True, revoked, " ".join(notes) or "Signed out.")


def list_accounts(accounts_dir):
    """Account profiles found in ``accounts_dir`` (``<label>.json`` files).

    Returns ``[{"label", "path", "status"}]`` sorted by label. Secrets are never
    returned.
    """
    directory = Path(accounts_dir)
    if not directory.is_dir():
        return []
    found = []
    for item in sorted(directory.glob("*.json")):
        name = item.name.lower()
        if name in _NON_ACCOUNT_FILES or name.startswith("client_secret"):
            continue
        found.append({"label": item.stem, "path": str(item), "status": credentials_status(item)})
    return found
