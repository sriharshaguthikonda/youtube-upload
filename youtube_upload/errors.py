"""Exception types shared by the CLI, the library code and the GUI."""


class YoutubeUploadError(Exception):
    """Base class for every error this package raises on purpose."""


class InvalidCategory(YoutubeUploadError):
    pass


class OptionsError(YoutubeUploadError):
    """Bad command-line options, bad input files or a bad configuration."""


class InvalidVideoFormat(YoutubeUploadError):
    pass


class AuthenticationError(YoutubeUploadError):
    """Signing in failed or the stored sign-in can no longer be used."""


class RequestError(YoutubeUploadError):
    """A YouTube API request failed."""


class QuotaExceeded(RequestError):
    """The API quota or the channel's upload limit is used up."""


class RetryExhausted(RequestError):
    """Transient errors persisted for longer than the retry policy allows."""


class UploadCancelled(YoutubeUploadError):
    """Raised when the user cancels via a progress callback or the GUI."""


class UploadFailed(YoutubeUploadError):
    """At least one requested upload did not complete.

    ``report`` is the :class:`youtube_upload.report.RunReport` with the
    per-account, per-video outcome.
    """

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report
