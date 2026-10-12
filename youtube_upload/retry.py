"""Error classification and bounded retry with capped, jittered backoff.

The old retry helper only compared ``type(exc)`` against a list, so subclasses
such as ``ConnectionResetError`` or ``httplib2.ServerNotFoundError`` were never
retried, 429 was never retried, and the wait doubled up to 1024 s per attempt.
This module replaces it:

* every exception is classified (:func:`classify_exception`);
* only transient failures are retried (network errors, 408/429/500/502/503/504
  and the rate-limit flavours of 403);
* authentication, quota and request errors are never retried;
* waits are capped, jittered, honour ``Retry-After`` and are cancellable;
* the failure counter resets after any success, and a wall-clock budget bounds
  how long one stretch of consecutive failures may last.
"""
import email.utils
import http.client
import json
import random
import socket
import ssl
import time
from collections import namedtuple
from datetime import datetime, timezone

import googleapiclient.errors
import httplib2

try:  # google-auth is a hard dependency, but keep this module importable alone
    import google.auth.exceptions as google_auth_exceptions
except ImportError:  # pragma: no cover
    google_auth_exceptions = None

from . import lib
from .errors import RetryExhausted, UploadCancelled

RETRY = "retry"
AUTH = "auth"
QUOTA = "quota"
NOT_FOUND = "not_found"
FATAL = "fatal"

Failure = namedtuple("Failure", "kind status reason message retry_after")

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
RATE_LIMIT_REASONS = {
    "ratelimitexceeded", "userratelimitexceeded", "backenderror", "internalerror",
    "servicenotavailable", "serviceunavailable",
}
QUOTA_REASONS = {
    "quotaexceeded", "dailylimitexceeded", "dailylimitexceededunreg",
    "uploadlimitexceeded", "quota_exceeded",
}
AUTH_REASONS = {
    "autherror", "forbidden", "insufficientpermissions", "accessnotconfigured",
    "youtubesignuprequired", "channelclosed", "channelsuspended", "unauthorized",
    "insufficientscopes", "access_denied",
}

# Local-file problems are never going to heal by waiting.
_NON_RETRYABLE_OS_ERRORS = (
    FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError,
)
_NETWORK_ERRORS = (
    ConnectionError,            # reset / aborted / refused / broken pipe
    TimeoutError,
    socket.timeout,             # a separate class before Python 3.10
    socket.gaierror,
    ssl.SSLError,
    http.client.HTTPException,  # IncompleteRead, BadStatusLine, NotConnected ...
    httplib2.HttpLib2Error,     # ServerNotFoundError, RedirectLimit ...
)


def _error_payload(exc):
    """Return ``(reason, message)`` from a Google API error body, if any."""
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    reason = message = None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None, (content.strip()[:300] or None)
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        message = err.get("message")
        errors = err.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = errors[0].get("reason")
        if not reason:
            for detail in err.get("details") or []:
                if isinstance(detail, dict) and detail.get("reason"):
                    reason = detail["reason"]
                    break
    elif isinstance(err, str):
        message = err
    return reason, message


def parse_retry_after(value, now=None):
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP date)."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (when - current).total_seconds())


def classify_exception(exc):
    """Classify ``exc`` into a :class:`Failure` (see module docstring)."""
    if isinstance(exc, googleapiclient.errors.HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
        reason, api_message = _error_payload(exc)
        key = (reason or "").lower()
        resp = getattr(exc, "resp", None)
        retry_after = None
        if resp is not None and hasattr(resp, "get"):
            retry_after = parse_retry_after(resp.get("retry-after"))
        text = "HTTP {0}".format(status if status is not None else "?")
        if reason:
            text += " ({0})".format(reason)
        if api_message:
            text += ": {0}".format(api_message)

        if key in QUOTA_REASONS:
            kind = QUOTA
        elif key in RATE_LIMIT_REASONS and status in (400, 403, 429, 500, 503):
            kind = RETRY
        elif status in RETRY_STATUSES:
            kind = RETRY
        elif status == 401:
            kind = AUTH
        elif status == 403:
            kind = AUTH if key in AUTH_REASONS else FATAL
        elif status in (404, 410):
            kind = NOT_FOUND
        else:
            kind = FATAL
        return Failure(kind, status, reason, text, retry_after)

    if google_auth_exceptions is not None:
        if isinstance(exc, google_auth_exceptions.TransportError):
            return Failure(RETRY, None, None, "network error while refreshing sign-in: {0}".format(exc), None)
        if isinstance(exc, google_auth_exceptions.RefreshError):
            return Failure(AUTH, None, None, "sign-in could not be refreshed: {0}".format(exc), None)

    if isinstance(exc, UploadCancelled):
        return Failure(FATAL, None, None, str(exc), None)
    if isinstance(exc, _NON_RETRYABLE_OS_ERRORS):
        return Failure(FATAL, None, None, "{0}: {1}".format(type(exc).__name__, exc), None)
    if isinstance(exc, _NETWORK_ERRORS) or isinstance(exc, OSError):
        return Failure(RETRY, None, None, "{0}: {1}".format(type(exc).__name__, exc or "-"), None)
    return Failure(FATAL, None, None, "{0}: {1}".format(type(exc).__name__, exc), None)


class RetryPolicy(object):
    """Bounds for :class:`Retrier`. All times are seconds."""

    def __init__(self, max_retries=8, base_delay=1.0, max_delay=60.0,
                 max_total_wait=900.0, max_retry_after=300.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_total_wait = max_total_wait
        self.max_retry_after = max_retry_after

    def delay(self, attempt, retry_after=None, rand=random.random):
        """Seconds to wait before retry number ``attempt`` (1-based).

        Returns ``None`` when the server asks for a wait longer than
        ``max_retry_after`` (the caller should give up rather than hang).
        """
        capped = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        jittered = capped / 2.0 + rand() * capped / 2.0
        if retry_after is None:
            return jittered
        if retry_after > self.max_retry_after:
            return None
        return max(jittered, retry_after)


class Retrier(object):
    """Run callables, retrying transient failures under a :class:`RetryPolicy`."""

    def __init__(self, policy=None, cancel_check=None, log=lib.debug,
                 sleep=time.sleep, monotonic=time.monotonic, rand=random.random):
        self.policy = policy or RetryPolicy()
        self.cancel_check = cancel_check
        self.log = log
        self._sleep = sleep
        self._monotonic = monotonic
        self._rand = rand
        self.attempt = 0
        self.waits = []  # planned wait before each retry, for diagnostics and tests
        self._first_failure_at = None

    def reset(self):
        self.attempt = 0
        self._first_failure_at = None

    def _check_cancel(self):
        if self.cancel_check and self.cancel_check():
            raise UploadCancelled("Cancelled by user")

    def _wait(self, seconds):
        """Sleep in short slices so a cancel request is honoured promptly."""
        deadline = self._monotonic() + seconds
        while True:
            self._check_cancel()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return
            self._sleep(min(0.25, remaining))

    def call(self, func, what="request"):
        while True:
            self._check_cancel()
            try:
                result = func()
            except Exception as exc:  # classified below; non-retryable ones re-raise
                failure = classify_exception(exc)
                if failure.kind != RETRY:
                    raise
                now = self._monotonic()
                if self._first_failure_at is None:
                    self._first_failure_at = now
                self.attempt += 1
                if self.attempt > self.policy.max_retries:
                    raise RetryExhausted(
                        "{0} still failing after {1} retries: {2}".format(
                            what, self.policy.max_retries, failure.message)) from exc
                wait = self.policy.delay(self.attempt, failure.retry_after, self._rand)
                if wait is None:
                    raise RetryExhausted(
                        "{0}: server asked to wait {1:.0f}s (limit {2:.0f}s): {3}".format(
                            what, failure.retry_after, self.policy.max_retry_after,
                            failure.message)) from exc
                spent = now - self._first_failure_at
                if spent + wait > self.policy.max_total_wait:
                    raise RetryExhausted(
                        "{0} kept failing for {1:.0f}s (limit {2:.0f}s): {3}".format(
                            what, spent, self.policy.max_total_wait, failure.message)) from exc
                self.log("[retry {0}/{1}] {2}: {3}. Waiting {4:.1f}s".format(
                    self.attempt, self.policy.max_retries, what, failure.message, wait))
                self.waits.append(wait)
                self._wait(wait)
                continue
            self.reset()
            return result
