"""Loopback (127.0.0.1) redirect server for the OAuth installed-app flow.

``InstalledAppFlow.run_local_server`` answers exactly one request, cannot be
cancelled, and its default handler logs every request line to stderr, which
would put the authorization code into logs. This replacement:

* accepts requests until one carries the expected ``state`` (stray requests
  such as a favicon fetch or a port probe are ignored, which also defeats
  forged redirects);
* can be cancelled and times out;
* never logs request lines;
* binds to 127.0.0.1 only.
"""
import html
import os
import secrets
import threading
import time
import webbrowser
import wsgiref.simple_server
from urllib.parse import parse_qs

from ..errors import AuthenticationError, UploadCancelled

_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>{title}</title></head>"
    "<body style='font-family:sans-serif;margin:3em'><h2>{title}</h2><p>{body}</p></body></html>"
)

# oauthlib raises on a "changed" scope (e.g. the user un-ticked one box on the
# consent screen) before we can report which one; we check explicitly instead.
_ENV_LOCK = threading.Lock()


class _QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - signature fixed by base class
        return  # request lines contain the authorization code


class _CallbackApp(object):
    def __init__(self, expected_state):
        self.expected_state = expected_state
        self.result = None  # ("code", value) or ("error", value)

    def _reply(self, start_response, status, title, body):
        payload = _PAGE.format(title=html.escape(title), body=html.escape(body)).encode("utf-8")
        start_response(status, [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            ("Cache-Control", "no-store"),
        ])
        return [payload]

    def __call__(self, environ, start_response):
        if environ.get("PATH_INFO", "/") != "/":
            return self._reply(start_response, "404 Not Found", "Not found", "Nothing here.")
        query = parse_qs(environ.get("QUERY_STRING", ""))
        state = (query.get("state") or [""])[0]
        if not secrets.compare_digest(state.encode("utf-8"), self.expected_state.encode("utf-8")):
            return self._reply(start_response, "400 Bad Request", "Unexpected request",
                               "This request did not come from the sign-in in progress.")
        if self.result is not None:
            return self._reply(start_response, "200 OK", "Already received",
                               "You can close this window.")
        if "error" in query:
            self.result = ("error", query["error"][0])
            return self._reply(start_response, "200 OK", "Sign-in was not completed",
                               "Return to the application and try again.")
        if "code" in query:
            self.result = ("code", query["code"][0])
            return self._reply(start_response, "200 OK", "Signed in",
                               "You can close this window and return to the application.")
        return self._reply(start_response, "400 Bad Request", "Bad request",
                           "No authorization code was supplied.")


def _granted_scopes(flow, credentials):
    """Scopes Google actually granted (not the ones we asked for).

    ``credentials.scopes`` echoes the *requested* scopes; the grant is in the
    token response, which oauthlib keeps on the session.
    """
    token = getattr(getattr(flow, "oauth2session", None), "token", None) or {}
    scope = token.get("scope")
    if isinstance(scope, str):
        scope = scope.split()
    if scope:
        return set(scope)
    return set(getattr(credentials, "granted_scopes", None) or credentials.scopes or [])


def run_loopback_flow(flow, required_scopes, open_browser=True, notify=None,
                      cancel=None, timeout=300, poll=0.25, browser_opener=None,
                      monotonic=time.monotonic, port=0):
    """Run the browser sign-in for ``flow`` and return its credentials.

    ``flow`` is a ``google_auth_oauthlib.flow.Flow`` (or a test double with the
    same ``redirect_uri``, ``authorization_url``, ``fetch_token`` and
    ``credentials`` members). ``cancel`` is an optional ``() -> bool``.
    """
    notify = notify or (lambda message: None)
    opener = browser_opener or (lambda url: webbrowser.open(url, new=1, autoraise=True))

    state = secrets.token_urlsafe(24)
    app = _CallbackApp(state)
    try:
        server = wsgiref.simple_server.make_server(
            "127.0.0.1", port, app, handler_class=_QuietHandler)
    except OSError as exc:
        raise AuthenticationError(
            "Cannot listen on 127.0.0.1:{0} for the Google sign-in redirect ({1}). Pick another "
            "--auth-port or leave it out.".format(port, exc)) from exc
    try:
        server.timeout = poll
        flow.redirect_uri = "http://127.0.0.1:{0}/".format(server.server_port)
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent", state=state)

        opened = False
        if open_browser:
            try:
                opened = bool(opener(auth_url))
            except Exception as exc:  # no usable browser; fall back to the URL
                notify("Could not open a browser automatically: {0}".format(exc))
        if opened:
            notify("A browser window was opened to sign in to Google. If it did not "
                   "appear, open this address yourself:\n{0}".format(auth_url))
        else:
            notify("Open this address in a browser on this computer to sign in "
                   "to Google:\n{0}".format(auth_url))

        deadline = monotonic() + timeout
        while app.result is None:
            if cancel and cancel():
                raise UploadCancelled("Sign-in cancelled")
            if monotonic() > deadline:
                raise AuthenticationError(
                    "Timed out after {0:.0f}s waiting for the Google sign-in to finish.".format(timeout))
            server.handle_request()
    finally:
        server.server_close()

    kind, value = app.result
    if kind == "error":
        if value == "access_denied":
            raise AuthenticationError("Google sign-in was cancelled or access was denied.")
        raise AuthenticationError("Google sign-in failed: {0}".format(value))

    with _ENV_LOCK:
        previous = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        try:
            flow.fetch_token(code=value)
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(
                "Google rejected the sign-in code: {0}: {1}".format(type(exc).__name__, exc)) from exc
        finally:
            if previous is None:
                os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
            else:
                os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous

    credentials = flow.credentials
    granted = _granted_scopes(flow, credentials)
    missing = sorted(set(required_scopes) - granted)
    if missing:
        raise AuthenticationError(
            "Not all required permissions were granted (missing: {0}). Sign in again and "
            "tick every permission box on the Google consent screen.".format(", ".join(missing)))
    if not credentials.refresh_token:
        raise AuthenticationError(
            "Google did not return a long-lived sign-in (no refresh token). Remove this app at "
            "https://myaccount.google.com/permissions and sign in again.")
    return credentials
