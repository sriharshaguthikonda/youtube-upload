#!/usr/bin/env python
#
# Upload videos to Youtube from the command-line using APIv3.
#
# Author: Arnau Sanchez <pyarnau@gmail.com>
# Project: https://github.com/tokland/youtube-upload
"""
Upload a video to Youtube from the command-line.

    $ youtube-upload --title="A.S. Mutter playing" \
                     --description="Anne Sophie Mutter plays Beethoven" \
                     --category=Music \
                     --tags="mutter, beethoven" \
                     anne_sophie_mutter.flv
    pxzZ-fYjeYs

Standard output carries only video ids (one per line); progress, warnings and
the final summary go to standard error. The exit status is 0 only if every
requested upload (and requested follow-up) succeeded.
"""

import optparse
import re
import sys
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import VERSION
from . import auth
from . import categories
from . import lib
from . import paths
from . import report as report_mod
from . import titles
from . import uploader
from .errors import (AuthenticationError, InvalidCategory, InvalidVideoFormat, OptionsError,
                     RequestError, UploadCancelled, UploadFailed, YoutubeUploadError)
from .formats import SUPPORTED_VIDEO_EXTENSIONS, validate_video_format  # noqa: F401 (re-exported)
from .progress import ProgressInfo, get_progress_info  # noqa: F401 (re-exported)
from .titles import default_title_from_video_path, sanitize_title  # noqa: F401 (re-exported)
from .uploader import WATCH_VIDEO_URL, create_internet_shortcut  # noqa: F401 (re-exported)

import googleapiclient.errors
from google.auth.exceptions import RefreshError
from . import retry

EXIT_CODES = {
    YoutubeUploadError: 1,
    OptionsError: 2,
    InvalidVideoFormat: 2,
    InvalidCategory: 3,
    RequestError: 3,
    AuthenticationError: 4,
    NotImplementedError: 5,
    UploadCancelled: 6,
    UploadFailed: lambda exc: (exc.report.exit_code() if exc.report is not None
                               else report_mod.EXIT_PARTIAL),
}

debug = lib.debug
struct = namedtuple
DEBUG_ENABLED = False
PROGRESS_FACTORY = None
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
MAX_LOG_BYTES = 5 * 1024 * 1024
PRIVACY_VALUES = ("public", "unlisted", "private")


# ---------------------------------------------------------------- small helpers

def log_debug(message):
    """Append to the debug log file (only when ``--debug`` is on). Never raises."""
    if not DEBUG_ENABLED:
        return
    debug(message)
    _append_log_file(message)


def _append_log_file(message):
    try:
        logfile = paths.app_home() / "youtube_upload_debug.log"
        logfile.parent.mkdir(parents=True, exist_ok=True)
        if logfile.exists() and logfile.stat().st_size > MAX_LOG_BYTES:
            logfile.replace(logfile.with_suffix(".log.1"))
        with open(str(logfile), "a", encoding="utf-8", newline="\n") as handle:
            handle.write(lib.redact(message) + "\n")
    except OSError:
        pass  # logging must never break an upload


def _make_logger(options, base):
    if not getattr(options, "debug", False):
        return base

    def log(message):
        base(message)
        _append_log_file(message)
    return log


def open_link(url):
    """Opens a URL link in the client's browser."""
    import webbrowser
    webbrowser.open(url)


def set_progress_factory(factory):
    """Register a callable video_path -> ProgressInfo(callback, finish) (used by the GUI)."""
    global PROGRESS_FACTORY
    PROGRESS_FACTORY = factory


def get_category_id(category):
    """Return category ID from its name."""
    if category:
        if category in categories.IDS:
            ncategory = categories.IDS[category]
            debug("Using category: {0} (id={1})".format(category, ncategory))
            return str(ncategory)
        raise InvalidCategory("{0} is not a valid category".format(category))


# ------------------------------------------------------------ option validation

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")


def normalize_datetime(value, name, warn=debug, must_be_future=False):
    """Validate an ISO 8601 timestamp; assume UTC (and say so) if no zone is given."""
    text = value.strip()
    match = _ISO_RE.match(text)
    if not match:
        raise OptionsError("{0} must be ISO 8601, e.g. 2026-01-31T12:30:00Z (got {1!r}).".format(name, value))
    try:
        naive = datetime(*[int(g) for g in match.groups()[:6]])
    except ValueError as exc:
        raise OptionsError("{0} is not a real date/time ({1}).".format(name, exc)) from exc
    zone = match.group(8)
    if not zone:
        warn("{0} has no time zone; assuming UTC ('Z'): {1}Z".format(name, text))
        text += "Z"
        aware = naive.replace(tzinfo=timezone.utc)
    elif zone == "Z":
        aware = naive.replace(tzinfo=timezone.utc)
    else:
        sign = 1 if zone[0] == "+" else -1
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        if hours > 23 or minutes > 59:
            raise OptionsError("{0} has an invalid time zone offset {1!r}.".format(name, zone))
        aware = naive.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
    if must_be_future and aware <= datetime.now(timezone.utc):
        warn("{0} is in the past; YouTube may publish the video immediately.".format(name))
    return text


def parse_location(text):
    """``latitude=VAL,longitude=VAL[,altitude=VAL]`` -> dict of floats."""
    try:
        values = lib.string_to_dict(text)
    except ValueError as exc:
        raise OptionsError("Invalid --location: {0}".format(exc)) from exc
    if not values:
        return None
    unknown = sorted(set(values) - {"latitude", "longitude", "altitude"})
    if unknown:
        raise OptionsError("Invalid --location: unknown key(s) {0}.".format(", ".join(unknown)))
    try:
        numbers = {key: float(val) for key, val in values.items()}
    except ValueError as exc:
        raise OptionsError("Invalid --location: values must be numbers ({0}).".format(exc)) from exc
    if "latitude" not in numbers or "longitude" not in numbers:
        raise OptionsError("Invalid --location: both latitude and longitude are required.")
    if not -90 <= numbers["latitude"] <= 90 or not -180 <= numbers["longitude"] <= 180:
        raise OptionsError("Invalid --location: latitude must be -90..90 and longitude -180..180.")
    return numbers


def parse_bool(value, name):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "on"):
        return True
    if text in ("false", "0", "no", "n", "off"):
        return False
    raise OptionsError("{0} must be True or False (got {1!r}).".format(name, value))


def validate_thumbnail(path):
    thumb = Path(path)
    if not thumb.is_file():
        raise OptionsError("Thumbnail file not found: {0}".format(path))
    if thumb.stat().st_size > MAX_THUMBNAIL_BYTES:
        raise OptionsError("Thumbnail {0} is larger than YouTube's 2 MB limit.".format(path))
    with thumb.open("rb") as handle:
        head = handle.read(8)
    if not (head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n")):
        raise OptionsError("Thumbnail {0} is not a JPEG or PNG image.".format(path))


def account_labels(options):
    """Account labels requested on the command line (``[None]`` = the default account)."""
    if options.accounts:
        labels = [label.strip() for label in options.accounts.split(",") if label.strip()]
        if not labels:
            raise OptionsError("--accounts was given but contains no account names.")
    elif options.account:
        labels = [options.account]
    else:
        return [None]
    seen = []
    for label in labels:
        paths.validate_account_label(label)
        if label not in seen:
            seen.append(label)
    return seen


def validate_options(options, args, need_videos=True, warn=debug):
    """Check everything that can be checked without the network. Raises OptionsError."""
    if need_videos and not args:
        raise OptionsError("No video files given. Usage: youtube-upload [OPTIONS] VIDEO [VIDEO2 ...]")
    if options.privacy not in PRIVACY_VALUES:
        raise OptionsError("--privacy must be one of: {0} (got {1!r}).".format(
            ", ".join(PRIVACY_VALUES), options.privacy))
    labels = account_labels(options)
    if options.credentials_file and len(labels) > 1:
        raise OptionsError(
            "--credentials-file names ONE sign-in and cannot be combined with several accounts "
            "(it would silently upload everything to the same channel). Use --accounts-dir with "
            "one <label>.json per account instead.")
    options.embeddable = parse_bool(options.embeddable, "--embeddable")
    if options.publish_at:
        options.publish_at = normalize_datetime(options.publish_at, "--publish-at", warn, must_be_future=True)
    if options.recording_date:
        options.recording_date = normalize_datetime(options.recording_date, "--recording-date", warn)
    options.location = parse_location(options.location) if isinstance(options.location, str) else options.location
    if options.category:
        get_category_id(options.category)
    if options.thumb:
        validate_thumbnail(options.thumb)
    if options.chunksize < 256 * 1024 or options.chunksize % (256 * 1024):
        raise OptionsError("--chunksize must be a positive multiple of 262144 bytes (256 KiB).")
    if options.max_retries < 0:
        raise OptionsError("--max-retries cannot be negative.")
    if not 0 <= options.auth_port <= 65535:
        raise OptionsError("--auth-port must be between 0 and 65535.")
    if options.description_file is not None:
        try:
            # utf-8-sig: Windows Notepad writes a BOM, which would end up in the description.
            with open(options.description_file, encoding="utf-8-sig") as handle:
                options.description = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise OptionsError("Cannot read --description-file {0}: {1}".format(
                options.description_file, exc)) from exc
    try:
        options.title_template.format(title="x", n=1, total=2)
    except (KeyError, IndexError, ValueError) as exc:
        raise OptionsError("Invalid --title-template {0!r}: {1}".format(options.title_template, exc)) from exc
    options.description = titles.clean_description(options.description, warn)
    options.tags = titles.clean_tags(options.tags, warn)
    return labels


# ------------------------------------------------------------ account resolution

AccountContext = namedtuple("AccountContext", "label client_secrets credentials")

def _legacy_default_credentials():
    """Where earlier versions kept the default account's sign-in (checked lazily: no home is fine)."""
    home = paths.home_dir()
    found = [home / ".youtube-upload-credentials.json"] if home else []
    found.append(Path(__file__).resolve().parent / ".youtube-upload-credentials.json")
    return tuple(found)


def _client_secrets_candidates(accounts_dir):
    pkg_dir = Path(__file__).resolve().parent
    candidates = []
    if accounts_dir is not None:
        candidates.append((accounts_dir / "client_secrets.json", False))
    candidates.append((paths.app_home() / "client_secrets.json", False))
    if paths.home_dir() is not None:
        candidates.append((paths.home_dir() / "client_secrets.json", False))
    # Deprecated: inside the source tree, where it can be committed by accident.
    candidates.append((pkg_dir / "client_secrets.json", True))
    candidates.append((pkg_dir.parent / "client_secrets.json", True))
    return candidates


def resolve_account(options, label, notify=debug):
    """Work out the client-secrets and credentials files for one account label."""
    if label:
        paths.validate_account_label(label)
    accounts_dir = None
    if options.accounts_dir:
        accounts_dir = Path(options.accounts_dir).expanduser()
    elif label:
        accounts_dir = paths.default_accounts_dir()

    client_secrets = None
    if options.client_secrets:
        client_secrets = Path(options.client_secrets).expanduser()
    else:
        tried = []
        for candidate, deprecated in _client_secrets_candidates(accounts_dir):
            tried.append(str(candidate))
            if candidate.is_file():
                client_secrets = candidate
                if deprecated:
                    notify("Warning: {0} is inside the source tree; move it to {1} "
                           "so it cannot be committed by accident.".format(
                               candidate, paths.app_home() / "client_secrets.json"))
                break
        if client_secrets is None:
            raise OptionsError(
                "client_secrets.json not found. Looked in: {0}. Create a Desktop-app OAuth "
                "client in Google Cloud Console, download it, and save it in one of those "
                "places or pass --client-secrets (see README).".format("; ".join(tried)))

    if options.credentials_file:
        credentials = Path(options.credentials_file).expanduser()
        shown = label or credentials.stem
    elif label or accounts_dir is not None:
        shown = label or "default"
        # No mkdir here: resolving must not create directories (the GUI resolves on every
        # keystroke); writing the credentials creates the folder when it is actually needed.
        credentials = accounts_dir / "{0}.json".format(shown)
    else:
        shown = "default"
        credentials = paths.app_home() / "credentials.json"
        _migrate_legacy_default(credentials, notify)
    return AccountContext(shown, client_secrets, credentials)


def _migrate_legacy_default(target, notify):
    """Copy a sign-in saved by older versions to the per-user location (non-destructive)."""
    if target.exists():
        return
    for legacy in _legacy_default_credentials():
        if legacy.is_file():
            try:
                creds, _ = auth.load_stored_credentials(legacy)
                if creds is not None:
                    auth.save_credentials(creds, target)
                    notify("Copied the saved sign-in from {0} to {1}. You can delete the old "
                           "file.".format(legacy, target))
                    return
            except (AuthenticationError, OSError) as exc:
                notify("Could not migrate the old sign-in {0}: {1}".format(legacy, exc))


def open_account(options, label=None, interactive=True, notify=debug, cancel=None):
    """Resolve and sign in one account. Returns ``(AccountContext, youtube_resource)``."""
    label = label if label is not None else options.account
    context = resolve_account(options, label, notify)
    debug("Using client secrets: {0}".format(context.client_secrets))
    debug("Using credentials file: {0}".format(context.credentials))
    youtube = auth.get_resource(
        str(context.client_secrets), str(context.credentials), interactive=interactive,
        open_browser=not options.no_browser, notify=notify, cancel=cancel,
        timeout=options.auth_timeout, port=options.auth_port)
    return context, youtube


def get_youtube_handler(options, label=None, **kwargs):
    """Return the API YouTube object for one account (signing in if needed)."""
    return open_account(options, label, **kwargs)[1]


# ------------------------------------------------------------------- the run

class _NullWriter(object):
    def write(self, *_args, **_kwargs):
        return None

    def flush(self):
        return None


def _notifying_add(run_report, on_result, log):
    plain_add = run_report.add

    def add(result):
        plain_add(result)
        if on_result is not None:
            try:
                on_result(result)
            except Exception as exc:  # a broken listener must not stop an upload run
                log("Internal error in result listener: {0}".format(exc))
        return result
    return add


def _fail_account(report, label, videos, exc_message, exit_code):
    report.account_failed(label, exc_message, exit_code)
    for path in videos:
        result = report_mod.VideoResult(label or "default", path)
        result.status = report_mod.NOT_ATTEMPTED
        result.message = "account could not be used: " + exc_message
        result.exit_code = exit_code
        report.add(result)


def _setup_failure(exc):
    """(message, exit code) for an error that happened before any upload started."""
    if isinstance(exc, (OptionsError, InvalidVideoFormat, InvalidCategory)):
        return str(exc), report_mod.EXIT_OPTIONS
    if isinstance(exc, AuthenticationError):
        return str(exc), report_mod.EXIT_AUTH
    if isinstance(exc, RefreshError):
        return auth.describe_refresh_error(exc) + " Sign in again.", report_mod.EXIT_AUTH
    if isinstance(exc, googleapiclient.errors.HttpError):
        failure = retry.classify_exception(exc)
        code = report_mod.EXIT_AUTH if failure.kind == retry.AUTH else report_mod.EXIT_REQUEST
        return failure.message, code
    return str(exc) or type(exc).__name__, report_mod.EXIT_REQUEST


def run_main(parser, options, args, output=sys.stdout, progress_factory=None,
             cancel_check=None, log=debug, notify=None, on_result=None, confirm_create_playlist=None):
    """Upload ``args`` to every requested account. Returns a RunReport; never hides failures.

    ``on_result(VideoResult)`` is called as each file finishes (the GUI uses it to
    update its per-file status); an exception inside it is logged, never fatal.
    """
    if output is None:  # pythonw (windowless GUI launch) has no stdout
        output = _NullWriter()
    notify = notify or log
    labels = validate_options(options, args, warn=log)
    log = _make_logger(options, log)
    progress_factory = progress_factory or PROGRESS_FACTORY

    run_report = report_mod.RunReport()
    run_report.add = _notifying_add(run_report, on_result, log)
    _warn_duplicate_titles(options, args, log)

    for label_index, label in enumerate(labels):
        shown = label or "default"
        if label:
            log("Starting uploads for account: {0}".format(label))
        try:
            context, youtube = open_account(
                options, label, interactive=True, notify=notify, cancel=cancel_check)
            shown = context.label
            account = uploader.AccountUploader(
                youtube, options, shown, paths.state_dir(), progress_factory=progress_factory,
                cancel_check=cancel_check, log=log,
                retry_policy=retry.RetryPolicy(max_retries=options.max_retries),
                confirm_create_playlist=confirm_create_playlist)
        except UploadCancelled:
            run_report.cancelled = True
            _mark_remaining(run_report, labels[label_index:], args, report_mod.CANCELLED, "cancelled")
            break
        except Exception as exc:  # account-level problem: isolate it, keep going
            message, code = _setup_failure(exc)
            log("Account {0} cannot be used: {1}".format(shown, message))
            _fail_account(run_report, shown, args, message, code)
            continue

        aborted = None
        for index, video_path in enumerate(args):
            if aborted:
                skipped = report_mod.VideoResult(shown, video_path)
                skipped.status = report_mod.NOT_ATTEMPTED
                skipped.message = "not attempted: " + aborted[0]
                skipped.exit_code = aborted[1]
                run_report.add(skipped)
                continue
            try:
                result = account.upload_one(video_path, index, len(args))
            except UploadCancelled:
                run_report.cancelled = True
                cancelled = report_mod.VideoResult(shown, video_path)
                cancelled.status = report_mod.CANCELLED
                cancelled.message = "cancelled"
                run_report.add(cancelled)
                _mark_remaining_videos(run_report, shown, args[index + 1:], report_mod.CANCELLED, "cancelled")
                _mark_remaining(run_report, labels[label_index + 1:], args, report_mod.CANCELLED, "cancelled")
                return _finish(run_report, log)
            run_report.add(result)
            if result.video_id and result.status in (report_mod.UPLOADED, report_mod.SKIPPED):
                output.write(result.video_id + "\n")
                output.flush()
            if result.abort_account:
                aborted = (result.message, result.exit_code)
    return _finish(run_report, log)


def _mark_remaining_videos(run_report, label, videos, status, message):
    for path in videos:
        result = report_mod.VideoResult(label, path)
        result.status = status
        result.message = message
        run_report.add(result)


def _mark_remaining(run_report, labels, videos, status, message):
    for label in labels:
        _mark_remaining_videos(run_report, label or "default", videos, status, message)


def _finish(run_report, log):
    for line in run_report.summary_lines():
        log(line)
    return run_report


def _warn_duplicate_titles(options, args, log):
    if len(args) < 2:
        return
    seen, dupes = {}, set()
    for index, path in enumerate(args):
        try:
            title = uploader.resolve_title(options, path, index, len(args), lambda message: None)
        except OptionsError:
            continue
        if title in seen:
            dupes.add(title)
        seen[title] = True
    if dupes:
        log("Note: several videos will get the same title: {0}".format(
            ", ".join(repr(t) for t in sorted(dupes))))


# --------------------------------------------------------------------- the CLI

def build_parser():
    usage = """Usage: %prog [OPTIONS] VIDEO [VIDEO2 ...]

    Upload videos to Youtube."""
    parser = optparse.OptionParser(usage, version="%prog " + VERSION)

    # Video metadata
    parser.add_option('-t', '--title', dest='title', type="string",
                      help='Video title (default: derived from each file name)')
    parser.add_option('-c', '--category', dest='category', type="string",
                      help='Name of video category')
    parser.add_option('-d', '--description', dest='description', type="string",
                      help='Video description')
    parser.add_option('', '--description-file', dest='description_file', type="string",
                      help='Video description file', default=None)
    parser.add_option('', '--tags', dest='tags', type="string",
                      help='Video tags (separated by commas: "tag1, tag2,...")')
    parser.add_option('', '--privacy', dest='privacy', metavar="STRING",
                      default="public", help='Privacy status (public | unlisted | private)')
    parser.add_option('', '--publish-at', dest='publish_at', metavar="datetime",
                      default=None, help='Publish date (ISO 8601): YYYY-MM-DDThh:mm:ss.sZ')
    parser.add_option('', '--license', dest='license', metavar="string",
                      choices=('youtube', 'creativeCommon'), default='youtube',
                      help='License for the video, either "youtube" (the default) or "creativeCommon"')
    parser.add_option('', '--location', dest='location', type="string",
                      default=None, metavar="latitude=VAL,longitude=VAL[,altitude=VAL]",
                      help='Video location"')
    parser.add_option('', '--recording-date', dest='recording_date', metavar="datetime",
                      default=None, help="Recording date (ISO 8601): YYYY-MM-DDThh:mm:ss.sZ")
    parser.add_option('', '--default-language', dest='default_language', type="string",
                      default=None, metavar="string",
                      help="Default language (ISO 639-1: en | fr | de | ...)")
    parser.add_option('', '--default-audio-language', dest='default_audio_language', type="string",
                      default=None, metavar="string",
                      help="Default audio language (ISO 639-1: en | fr | de | ...)")
    parser.add_option('', '--thumbnail', dest='thumb', type="string", metavar="FILE",
                      help='Image file to use as video thumbnail (JPEG or PNG, max 2 MB)')
    parser.add_option('', '--playlist', dest='playlist', type="string",
                      help='Playlist title (if it does not exist, it will be created)')
    parser.add_option('', '--title-template', dest='title_template',
                      type="string", default="{title} [{n}/{total}]", metavar="string",
                      help='Template for several videos sharing one --title '
                           '(default: {title} [{n}/{total}])')
    parser.add_option('', '--embeddable', dest='embeddable', default=True,
                      help='Video is embeddable (True | False)')
    parser.add_option('', '--skip-if-exists', dest='skip_if_exists',
                      choices=('title', 'hash'), default=None,
                      help='Skip upload if the channel already has a video with the same title, '
                           'or the same file content (hash)')

    # Authentication and accounts
    parser.add_option('', '--client-secrets', dest='client_secrets',
                      type="string", help='Client secrets JSON file (Desktop-app OAuth client)')
    parser.add_option('', '--credentials-file', dest='credentials_file',
                      type="string", help='Credentials JSON file')
    parser.add_option('', '--account', dest='account', type="string",
                      help='Account label to isolate credentials (stored as <accounts-dir>/<label>.json)')
    parser.add_option('', '--accounts', dest='accounts', type="string",
                      help='Comma-separated list of account labels to upload to multiple accounts in one run')
    parser.add_option('', '--accounts-dir', dest='accounts_dir', type="string",
                      help='Directory with the per-account credentials (default: '
                           '~/.youtube-upload-accounts if it exists, else the per-user '
                           'application-data folder)')
    parser.add_option('', '--login', dest='login', action='store_true',
                      help='Sign in the chosen account(s) in the browser and exit')
    parser.add_option('', '--logout', dest='logout', action='store_true',
                      help='Sign out the chosen account(s): revoke the token and delete the saved sign-in')
    parser.add_option('', '--no-revoke', dest='no_revoke', action='store_true',
                      help='With --logout: only delete the local sign-in, do not contact Google')
    parser.add_option('', '--list-accounts', dest='list_accounts', action='store_true',
                      help='List the accounts found in the accounts directory and exit')
    parser.add_option('', '--no-browser', dest='no_browser', action='store_true',
                      help='Do not open a browser automatically; print the sign-in address instead')
    parser.add_option('', '--auth-timeout', dest='auth_timeout', type="int", default=300,
                      help='Seconds to wait for the browser sign-in (default 300)')
    parser.add_option('', '--auth-port', dest='auth_port', type="int", default=0,
                      help='Fixed local port for the sign-in redirect (default: any free port). '
                           'Use with an SSH tunnel: ssh -L PORT:127.0.0.1:PORT host')
    parser.add_option('', '--auth-browser', dest='auth_browser', action='store_true',
                      help='Deprecated and ignored: the system browser is now always used for sign-in')

    # Additional options
    parser.add_option('', '--chunksize', dest='chunksize', type="int",
                      default=1024 * 1024 * 8,
                      help='Upload chunk size in bytes, a multiple of 262144 (default 8 MiB)')
    parser.add_option('', '--max-retries', dest='max_retries', type="int", default=8,
                      help='Consecutive transient errors tolerated before giving up (default 8)')
    parser.add_option('', '--no-verify', dest='verify', action='store_false', default=True,
                      help='Do not ask YouTube to confirm the video after uploading')
    parser.add_option('', '--open-link', dest='open_link', action='store_true',
                      help='Opens a url in a web browser to display the uploaded video')
    parser.add_option('', '--debug', dest='debug', action='store_true',
                      help='Also write the log to youtube_upload_debug.log in the application-data folder')
    return parser


def _run_account_action(options, labels, action, notify):
    for label in labels:
        context = resolve_account(options, label, notify)
        action(context)


def main(arguments, progress_factory=None, cancel_check=None, notify=None, log=debug,
         output=sys.stdout, on_result=None, confirm_create_playlist=None):
    """Upload videos to Youtube. Returns the RunReport; raises on any failure.

    ``confirm_create_playlist(title) -> bool`` decides whether a missing playlist is created;
    the default asks on an interactive terminal (and creates silently when there is none).
    The GUI passes its own, because a worker thread must never block on ``input()``.
    """
    parser = build_parser()
    options, args = parser.parse_args(arguments)

    global DEBUG_ENABLED
    DEBUG_ENABLED = bool(options.debug)
    if DEBUG_ENABLED:
        debug("Debug logging enabled")
    if options.auth_browser:
        log("Note: --auth-browser is obsolete and ignored; sign-in always uses your system browser.")
    notify = notify or log

    if options.list_accounts:
        directory = (Path(options.accounts_dir).expanduser() if options.accounts_dir
                     else paths.default_accounts_dir())
        found = auth.list_accounts(directory)
        if not found:
            log("No accounts found in {0}".format(directory))
        for account in found:
            (output or _NullWriter()).write("{0}\t{1}\n".format(account["label"], account["status"]))
        return None

    if options.login or options.logout:
        labels = account_labels(options)

        def sign_in(context):
            auth.login(str(context.client_secrets), str(context.credentials),
                       open_browser=not options.no_browser, notify=notify,
                       cancel=cancel_check, timeout=options.auth_timeout, port=options.auth_port)
            log("Signed in: {0}".format(context.label))

        def sign_out(context):
            result = auth.logout(context.credentials, revoke=not options.no_revoke)
            log("{0}: {1}".format(context.label, result.message))

        _run_account_action(options, labels, sign_in if options.login else sign_out, notify)
        return None

    run_report = run_main(parser, options, args, output=output, progress_factory=progress_factory,
                          cancel_check=cancel_check, log=log, notify=notify, on_result=on_result,
                          confirm_create_playlist=confirm_create_playlist)
    if run_report.cancelled:
        raise UploadCancelled("Cancelled by user")
    if not run_report.ok:
        # The per-file summary was already logged by run_main; keep this message short so the
        # CLI does not print it twice (the GUI builds its dialog from run_report itself).
        failed = sum(1 for r in run_report.results if not r.succeeded) + len(run_report.account_errors)
        raise UploadFailed("{0} of {1} requested upload(s) did not complete; see the summary above.".format(
            max(failed, 1), max(len(run_report.results), 1)), run_report)
    return run_report


def run():
    try:
        code = lib.catch_exceptions(EXIT_CODES, main, sys.argv[1:])
    except KeyboardInterrupt:
        debug("Interrupted.")
        code = 130
    sys.exit(code)


if __name__ == '__main__':
    run()
