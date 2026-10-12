"""#15: bounded retries, correct classification, Retry-After, no retry of 4xx."""
import socket
import ssl

import httplib2
import pytest

from tests.conftest import make_http_error
from youtube_upload import retry
from youtube_upload.errors import RetryExhausted, UploadCancelled
from youtube_upload.retry import Retrier, RetryPolicy, classify_exception


def _retrier(clock, **policy):
    return Retrier(RetryPolicy(**policy), log=lambda m: None, sleep=clock.sleep,
                   monotonic=clock.monotonic, rand=lambda: 1.0)


def _run_until_exhausted(clock, status=503, **policy):
    retrier = _retrier(clock, **policy)
    with pytest.raises(RetryExhausted):
        retrier.call(_failing(10 ** 6, lambda: make_http_error(status)))
    return retrier


def _failing(times, exc_factory, result="ok"):
    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if state["calls"] <= times:
            raise exc_factory()
        return result
    fn.state = state
    return fn


# ---------------------------------------------------------------- classify

@pytest.mark.parametrize("status,reason,kind", [
    (500, None, retry.RETRY),
    (502, None, retry.RETRY),
    (503, "backendError", retry.RETRY),
    (504, None, retry.RETRY),
    (429, None, retry.RETRY),
    (408, None, retry.RETRY),
    (403, "rateLimitExceeded", retry.RETRY),
    (403, "userRateLimitExceeded", retry.RETRY),
    (403, "quotaExceeded", retry.QUOTA),
    (403, "dailyLimitExceeded", retry.QUOTA),
    (400, "uploadLimitExceeded", retry.QUOTA),
    (401, None, retry.AUTH),
    (403, "forbidden", retry.AUTH),
    (403, "insufficientPermissions", retry.AUTH),
    (403, "youtubeSignupRequired", retry.AUTH),
    (403, None, retry.FATAL),
    (400, "invalidTitle", retry.FATAL),
    (400, None, retry.FATAL),
    (404, None, retry.NOT_FOUND),
    (410, None, retry.NOT_FOUND),
    (501, None, retry.FATAL),
])
def test_http_error_classification(status, reason, kind):
    assert classify_exception(make_http_error(status, reason)).kind == kind


@pytest.mark.parametrize("exc", [
    ConnectionResetError("reset"),
    ConnectionAbortedError("aborted"),
    BrokenPipeError("pipe"),
    TimeoutError("timed out"),
    socket.timeout("timed out"),
    socket.gaierror("dns"),
    ssl.SSLError("tls"),
    httplib2.ServerNotFoundError("dns"),
    httplib2.HttpLib2Error("generic"),
    OSError("network is unreachable"),
])
def test_network_errors_are_retryable_including_subclasses(exc):
    # Regression: the old helper compared type(exc) with a list, so every
    # subclass (ConnectionResetError, ServerNotFoundError, ...) was fatal.
    assert classify_exception(exc).kind == retry.RETRY


@pytest.mark.parametrize("exc", [
    FileNotFoundError("missing.mp4"),
    PermissionError("denied"),
    IsADirectoryError("dir"),
    ValueError("bug"),
    KeyError("id"),
    UploadCancelled("cancelled"),
])
def test_local_and_programming_errors_are_not_retryable(exc):
    assert classify_exception(exc).kind == retry.FATAL


def test_classification_extracts_reason_and_message():
    failure = classify_exception(make_http_error(403, "quotaExceeded", "You exceeded your quota"))
    assert failure.reason == "quotaExceeded"
    assert "quota" in failure.message.lower()
    assert failure.status == 403


def test_classification_survives_non_json_body():
    import googleapiclient.errors
    exc = googleapiclient.errors.HttpError(httplib2.Response({"status": "502"}), b"<html>Bad gateway</html>")
    failure = classify_exception(exc)
    assert failure.kind == retry.RETRY
    assert "502" in failure.message


@pytest.mark.parametrize("value,expected", [
    ("7", 7.0), ("0", 0.0), ("2.5", 2.5), (None, None), ("garbage", None), ("-3", 0.0),
])
def test_parse_retry_after_seconds(value, expected):
    assert retry.parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    from datetime import datetime, timezone
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert retry.parse_retry_after("Thu, 01 Jan 2026 12:01:00 GMT", now=now) == 60.0
    assert retry.parse_retry_after("Thu, 01 Jan 2026 11:00:00 GMT", now=now) == 0.0


# ------------------------------------------------------------------ retries

def test_retries_connection_reset_then_succeeds(clock):
    fn = _failing(3, lambda: ConnectionResetError("reset"))
    assert _retrier(clock).call(fn) == "ok"
    assert fn.state["calls"] == 4
    assert len(clock.sleeps) > 0


def test_429_is_retried(clock):
    fn = _failing(2, lambda: make_http_error(429))
    assert _retrier(clock).call(fn) == "ok"
    assert fn.state["calls"] == 3


@pytest.mark.parametrize("status,reason", [
    (400, "invalidTitle"), (401, None), (403, "forbidden"), (403, "quotaExceeded"), (404, None),
])
def test_non_retryable_http_errors_are_raised_immediately(clock, status, reason):
    fn = _failing(5, lambda: make_http_error(status, reason))
    with pytest.raises(Exception) as info:
        _retrier(clock).call(fn)
    assert info.value.resp.status == status
    assert fn.state["calls"] == 1
    assert clock.sleeps == []


def test_file_errors_are_not_retried(clock):
    fn = _failing(5, lambda: FileNotFoundError("gone"))
    with pytest.raises(FileNotFoundError):
        _retrier(clock).call(fn)
    assert fn.state["calls"] == 1


def test_backoff_is_capped_at_max_delay(clock):
    # Old behaviour: waits doubled to 1024 s. Now every wait is <= 60 s.
    retrier = _run_until_exhausted(clock, max_retries=12, max_total_wait=10 ** 9)
    assert len(retrier.waits) == 12
    assert max(retrier.waits) <= 60.0
    assert retrier.waits[-1] == pytest.approx(60.0)  # rand()==1.0 -> full cap
    assert sum(clock.sleeps) == pytest.approx(sum(retrier.waits), abs=0.3)


def test_backoff_grows_then_plateaus(clock):
    retrier = _run_until_exhausted(clock, status=500, max_retries=8, max_total_wait=10 ** 9)
    assert retrier.waits == [1, 2, 4, 8, 16, 32, 60, 60]


def test_gives_up_after_max_retries_with_clear_error(clock):
    fn = _failing(99, lambda: make_http_error(503, message="try later"))
    with pytest.raises(RetryExhausted) as info:
        _retrier(clock, max_retries=3).call(fn, what="upload chunk")
    assert fn.state["calls"] == 4
    assert "upload chunk" in str(info.value)
    assert "503" in str(info.value)


def test_wall_clock_budget_bounds_consecutive_failures(clock):
    fn = _failing(99, lambda: make_http_error(503))
    with pytest.raises(RetryExhausted) as info:
        _retrier(clock, max_retries=1000, max_total_wait=100).call(fn)
    assert "limit 100s" in str(info.value)
    assert sum(clock.sleeps) <= 100 + 0.3


def test_retry_after_is_honoured(clock):
    fn = _failing(1, lambda: make_http_error(429, headers={"retry-after": "42"}))
    _retrier(clock).call(fn)
    assert sum(clock.sleeps) >= 42


def test_retry_after_longer_than_limit_gives_up_instead_of_hanging(clock):
    fn = _failing(1, lambda: make_http_error(429, headers={"retry-after": "3600"}))
    with pytest.raises(RetryExhausted) as info:
        _retrier(clock, max_retry_after=120).call(fn)
    assert "3600" in str(info.value)
    assert clock.sleeps == []


def test_failure_counter_resets_after_success(clock):
    retrier = _retrier(clock, max_retries=2)
    for _ in range(5):  # each call burns 2 retries; would exhaust if not reset
        fn = _failing(2, lambda: make_http_error(503))
        assert retrier.call(fn) == "ok"


def test_cancel_interrupts_the_wait(clock):
    state = {"n": 0}

    def cancel():
        state["n"] += 1
        return state["n"] > 3

    retrier = Retrier(RetryPolicy(base_delay=50, max_delay=50), cancel_check=cancel,
                      log=lambda m: None, sleep=clock.sleep, monotonic=clock.monotonic,
                      rand=lambda: 1.0)
    with pytest.raises(UploadCancelled):
        retrier.call(_failing(5, lambda: make_http_error(503)))
    assert sum(clock.sleeps) < 5  # did not sleep the whole 50 s


def test_cancel_before_first_attempt(clock):
    retrier = Retrier(cancel_check=lambda: True, log=lambda m: None,
                      sleep=clock.sleep, monotonic=clock.monotonic)
    fn = _failing(0, ValueError)
    with pytest.raises(UploadCancelled):
        retrier.call(fn)
    assert fn.state["calls"] == 0


def test_policy_delay_bounds():
    policy = RetryPolicy(base_delay=1, max_delay=60)
    for attempt in range(1, 30):
        low = policy.delay(attempt, rand=lambda: 0.0)
        high = policy.delay(attempt, rand=lambda: 1.0)
        cap = min(60, 2 ** (attempt - 1))
        assert low == pytest.approx(cap / 2.0)
        assert high == pytest.approx(cap)
        assert high <= 60
