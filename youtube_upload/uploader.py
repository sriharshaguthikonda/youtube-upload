"""Upload videos to one authenticated channel and report what really happened.

Per video: validate -> title -> content hash -> duplicate check -> resumable
upload -> record in the ledger -> verify with YouTube -> follow-ups (shortcut,
thumbnail, playlist). Failures are classified, never swallowed:

* a problem with one file fails that file only;
* sign-in, quota or exhausted-retry problems end the work for this account
  (continuing would only repeat the failure) and the remaining files are
  reported as *not attempted*;
* a requested follow-up that fails (thumbnail, playlist) is a *problem* on an
  otherwise uploaded video, so the exit code is not 0 but the video is not
  uploaded a second time on re-run.
"""
import time
import traceback
import webbrowser
from pathlib import Path

import googleapiclient.errors

from . import (channel as channel_mod, content_validation, formats, lib, playlists,
               report, retry, titles, upload_video)
from .errors import (AuthenticationError, InvalidCategory, InvalidVideoFormat, OptionsError,
                     QuotaExceeded, RequestError, RetryExhausted, UploadCancelled)
from .ledger import Ledger
from .progress import get_progress_info

WATCH_VIDEO_URL = "https://www.youtube.com/watch?v={id}"
# API error reasons meaning "the tags/keywords in the request are not acceptable".
INVALID_KEYWORD_REASONS = {"invalidtags", "invalidvideokeywords"}


def create_internet_shortcut(video_path, video_url):
    """Write ``<video file name>.url`` next to the video. Returns its path.

    Raises ``OSError`` if it cannot be written. (Earlier versions also wrote an
    identical ``.m3u`` file, which is not a valid playlist.)
    """
    path = Path(video_path)
    shortcut_path = path.parent / (path.name + ".url")
    content = "[InternetShortcut]\nURL={0}\n".format(video_url)
    with open(str(shortcut_path), "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(content)
    return shortcut_path


def resolve_title(options, video_path, index, total, warn):
    """Final title for one video.

    * explicit ``--title``: used as typed (minus characters YouTube forbids);
      with several videos the ``--title-template`` is applied;
    * no title: derived from the file name, one distinct title per file, with
      no ``[n/total]`` suffix (it would mislabel unrelated videos).
    """
    if options.title:
        base = titles.explicit_title(options.title, warn)
        if total > 1:
            try:
                combined = options.title_template.format(title=base, n=index + 1, total=total)
            except (KeyError, IndexError, ValueError) as exc:
                raise OptionsError("Invalid --title-template {0!r}: {1}".format(
                    options.title_template, exc)) from exc
            return titles.explicit_title(combined, warn)
        return base
    return titles.default_title_from_video_path(video_path, warn)


def build_tags(options, sha256=None):
    tags = [t.strip() for t in (options.tags or "").split(",") if t.strip()]
    if sha256 and options.skip_if_exists == "hash":
        tag = channel_mod.hash_tag(sha256)
        if tag not in tags:
            tags.append(tag)
    return tags


class AccountUploader(object):
    def __init__(self, youtube, options, label, state_dir, progress_factory=None,
                 cancel_check=None, log=lib.debug, retry_policy=None,
                 sleep=time.sleep, monotonic=time.monotonic, confirm_create_playlist=None):
        self.youtube = youtube
        self.options = options
        self.label = label
        self.log = log
        self.cancel_check = cancel_check
        self.progress_factory = progress_factory
        self.retrier = retry.Retrier(
            retry_policy or retry.RetryPolicy(max_retries=getattr(options, "max_retries", 8)),
            cancel_check=cancel_check, log=log, sleep=sleep, monotonic=monotonic)
        self.channel = channel_mod.get_channel(youtube, self.retrier)
        state_dir = Path(state_dir)
        self.ledger = Ledger(state_dir / "ledger", self.channel.id, warn=log)
        self.sessions = upload_video.SessionStore(state_dir / "sessions", warn=log)
        self.remote = channel_mod.RemoteIndex(youtube, self.channel, self.retrier, log=log)
        self._playlist_cache = {}
        self._confirm_create = confirm_create_playlist or playlists.confirm_create_on_terminal
        self._hashes = {}

    # ------------------------------------------------------------------ helpers

    def _sha256(self, path):
        stat = Path(path).stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in self._hashes:
            self._hashes[key] = lib.sha256_file(path, cancel_check=self.cancel_check)
        return self._hashes[key]

    def _video_still_exists(self, video_id):
        """True/False if YouTube says; ``None`` if it could not be checked."""
        try:
            response = self.retrier.call(
                lambda: self.youtube.videos().list(part="snippet", id=video_id).execute(),
                what="existing-video check")
        except (RetryExhausted, RequestError, googleapiclient.errors.HttpError) as exc:
            self.log("Could not confirm video {0} still exists ({1}); trusting the local record.".format(
                video_id, exc))
            return None
        items = response.get("items") or []
        return bool(items) and (items[0].get("snippet") or {}).get("channelId") == self.channel.id

    def _find_existing(self, sha256, title, result):
        mode = self.options.skip_if_exists
        entry = self.ledger.get(sha256)
        if entry and mode == "hash":
            still = self._video_still_exists(entry["video_id"])
            if still is not False:
                return entry["video_id"]
            self.log("Recorded video {0} no longer exists on this channel; uploading again.".format(
                entry["video_id"]))
            self.ledger.forget(sha256)
        elif entry and not mode:
            result.warnings.append(
                "This exact file was already uploaded to this channel as {0}; use "
                "--skip-if-exists hash to skip duplicates.".format(entry["video_id"]))
        if mode == "hash":
            return self.remote.find_by_hash(sha256)
        if mode == "title":
            return self.remote.find_by_title(title)
        return None

    def _body(self, title, tags, category_id):
        o = self.options
        snippet = {
            "title": title,
            "description": o.description,
            "categoryId": category_id,
            "tags": tags,
            "defaultLanguage": o.default_language,
            "defaultAudioLanguage": o.default_audio_language,
        }
        status = {
            "embeddable": o.embeddable,
            "privacyStatus": "private" if o.publish_at else o.privacy,
            "publishAt": o.publish_at,
            "license": o.license,
        }
        body = {"snippet": snippet, "status": status}
        details = {"location": o.location, "recordingDate": o.recording_date}
        if any(v is not None for v in details.values()):
            body["recordingDetails"] = details
        return body

    # --------------------------------------------------------------- one video

    def upload_one(self, video_path, index, total):
        """Process one file. Returns a :class:`report.VideoResult`.

        Raises only :class:`UploadCancelled`.
        """
        result = report.VideoResult(self.label, video_path)

        def note(message):
            result.warnings.append(message)
            self.log("Note: " + message)

        try:
            formats.validate_video_format(video_path)
            content_validation.validate_video_content(video_path, warn=note)
            title = resolve_title(self.options, video_path, index, total, note)
            category_id = _category_id(self.options.category)
            sha256 = self._sha256(video_path)
            size = Path(video_path).stat().st_size

            video_id = self._find_existing(sha256, title, result)
            if video_id:
                result.status = report.SKIPPED
                result.message = "already on the channel"
                self.log("Skipping upload; video already exists: {0}".format(video_id))
            else:
                video_id = self._upload(video_path, title, sha256, size, category_id, result)
                result.status = report.UPLOADED
            result.video_id = video_id
            result.url = WATCH_VIDEO_URL.format(id=video_id)
            self._follow_up(video_path, video_id, result)
        except UploadCancelled:
            raise
        except (InvalidVideoFormat, OptionsError, InvalidCategory) as exc:
            self._fail(result, exc, report.EXIT_OPTIONS)
        except AuthenticationError as exc:
            self._fail(result, exc, report.EXIT_AUTH, abort=True)
        except (QuotaExceeded, RetryExhausted) as exc:
            self._fail(result, exc, report.EXIT_REQUEST, abort=True)
        except RequestError as exc:
            self._fail(result, exc, report.EXIT_REQUEST)
        except googleapiclient.errors.HttpError as exc:
            failure = retry.classify_exception(exc)
            if failure.kind == retry.AUTH:
                self._fail(result, AuthenticationError(
                    failure.message + " (sign in again or check the account's YouTube permissions)"),
                    report.EXIT_AUTH, abort=True)
            elif failure.kind == retry.QUOTA:
                self._fail(result, QuotaExceeded(failure.message), report.EXIT_REQUEST, abort=True)
            else:
                self._fail(result, RequestError(failure.message), report.EXIT_REQUEST)
        except OSError as exc:
            self._fail(result, exc, report.EXIT_REQUEST)
        except Exception as exc:  # a bug: never hide it, never call it success
            self.log(traceback.format_exc())
            self._fail(result, RequestError("unexpected {0}: {1}".format(type(exc).__name__, exc)),
                       report.EXIT_REQUEST)
        return result

    def _fail(self, result, exc, exit_code, abort=False):
        result.status = report.FAILED
        result.message = str(exc) or type(exc).__name__
        result.exit_code = exit_code
        result.abort_account = abort
        self.log("FAILED {0}: {1}".format(result.path, result.message))

    def _upload_with_body(self, video_path, body, sha256, size):
        session = upload_video.UploadSession(
            self.sessions, upload_video.session_key(self.channel.id, sha256, size))
        progress = self.progress_factory(video_path) if self.progress_factory else get_progress_info()
        self.log("Start upload: {0}".format(video_path))
        try:
            return upload_video.upload(
                self.youtube, video_path, body, chunksize=self.options.chunksize,
                progress_callback=progress.callback, retrier=self.retrier,
                session=session, log=self.log)
        finally:
            progress.finish()

    def _upload(self, video_path, title, sha256, size, category_id, result):
        o = self.options
        tags = build_tags(o, sha256)
        body = self._body(title, tags, category_id)
        try:
            video_id = self._upload_with_body(video_path, body, sha256, size)
        except googleapiclient.errors.HttpError as exc:
            failure = retry.classify_exception(exc)
            our_tag = channel_mod.hash_tag(sha256)
            if (failure.reason or "").lower() not in INVALID_KEYWORD_REASONS or our_tag not in tags:
                raise
            # The duplicate-detection tag is this tool's own addition (69 characters). If the
            # API refuses the keywords, upload without it once; the local record still
            # prevents re-uploads from this computer.
            result.warnings.append(
                "YouTube rejected the duplicate-detection tag ({0}); uploading without it. The "
                "local upload record still prevents duplicates from this computer.".format(failure.message))
            self.log("Note: " + result.warnings[-1])
            body = self._body(title, [t for t in tags if t != our_tag], category_id)
            video_id = self._upload_with_body(video_path, body, sha256, size)

        # Record before anything else can fail so a re-run never uploads it again.
        try:
            self.ledger.record(sha256, video_id, title, size, Path(video_path).name)
        except (OSError, TimeoutError) as exc:
            result.warnings.append("Uploaded, but could not update the local upload record: {0}".format(exc))
        self.remote.add(video_id, title, sha256)

        if getattr(o, "verify", True):
            try:
                item = upload_video.verify_video(self.youtube, video_id, self.retrier)
            except RequestError as exc:
                if "reports video" in str(exc):  # YouTube rejected / failed it
                    self.ledger.forget(sha256)
                    raise
                result.warnings.append("Uploaded as {0} but could not be verified: {1}".format(video_id, exc))
            except googleapiclient.errors.HttpError as exc:
                result.warnings.append("Uploaded as {0} but could not be verified: {1}".format(
                    video_id, retry.classify_exception(exc).message))
            else:
                if o.skip_if_exists == "hash" and not any("rejected the duplicate-detection tag" in w
                                                          for w in result.warnings):
                    tags = (item.get("snippet") or {}).get("tags") or []
                    if channel_mod.hash_tag(sha256) not in tags:
                        result.warnings.append(
                            "YouTube did not keep the duplicate-detection tag; the local upload "
                            "record still prevents re-uploading from this computer.")
        return video_id

    def _follow_up(self, video_path, video_id, result):
        o = self.options
        if o.open_link:
            try:
                webbrowser.open(result.url)
            except Exception as exc:
                result.warnings.append("Could not open a browser: {0}".format(exc))
        try:
            create_internet_shortcut(video_path, result.url)
        except OSError as exc:
            result.warnings.append("Could not write the .url shortcut: {0}".format(exc))
        if o.thumb:
            try:
                self.retrier.call(
                    lambda: self.youtube.thumbnails().set(videoId=video_id, media_body=o.thumb).execute(),
                    what="thumbnail upload")
            except UploadCancelled:
                raise
            except Exception as exc:
                result.problems.append("thumbnail not set: {0}".format(_describe(exc)))
        if o.playlist:
            try:
                playlists.add_video_to_playlist(
                    self.youtube, video_id, o.playlist, privacy=o.privacy, retrier=self.retrier,
                    confirm_create=self._confirm_create, cache=self._playlist_cache)
            except UploadCancelled:
                raise
            except Exception as exc:
                result.problems.append("not added to playlist {0!r}: {1}".format(o.playlist, _describe(exc)))


def _describe(exc):
    if isinstance(exc, googleapiclient.errors.HttpError):
        return retry.classify_exception(exc).message
    return str(exc) or type(exc).__name__


def _category_id(category):
    from . import categories
    if not category:
        return None
    if category not in categories.IDS:
        raise InvalidCategory("{0} is not a valid category".format(category))
    return str(categories.IDS[category])
