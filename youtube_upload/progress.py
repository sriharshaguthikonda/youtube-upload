"""Terminal progress bar for the CLI (#5).

The PyPI distribution is called ``progressbar2`` but the module it installs is
``progressbar``. The old ``import progressbar2`` always failed, was swallowed,
and left the CLI without a bar.
"""
import sys
from collections import namedtuple

from . import lib

try:
    import progressbar
except ImportError:  # the package is a declared dependency; degrade loudly, not silently
    progressbar = None

ProgressInfo = namedtuple("ProgressInfo", "callback finish")

_warned_missing = False


def get_progress_info(stream=None, warn=lib.debug):
    """Return ``ProgressInfo(callback(total, done), finish())`` drawing on ``stream``.

    With no usable stream (``pythonw``) or no ``progressbar`` module the
    callback is ``None``; a missing module is reported once on stderr.
    """
    global _warned_missing
    stream = stream if stream is not None else sys.stderr
    if progressbar is None:
        if not _warned_missing:
            _warned_missing = True
            warn("Progress bar disabled: install it with  pip install progressbar2")
        return ProgressInfo(callback=None, finish=lambda: None)
    if stream is None:
        return ProgressInfo(callback=None, finish=lambda: None)

    state = {"bar": None, "done": False}

    def callback(total_size, completed):
        if state["bar"] is None:
            state["bar"] = progressbar.ProgressBar(
                max_value=max(total_size, 1),
                fd=stream,
                widgets=[
                    progressbar.Percentage(), " ", progressbar.Bar(), " ",
                    progressbar.FileTransferSpeed(), " ",
                    progressbar.DataSize(), "/", progressbar.DataSize("max_value"), " ",
                    progressbar.Timer(), " ", progressbar.AdaptiveETA(),
                ])
            state["bar"].start()
        state["bar"].update(min(completed, max(total_size, 1)))
        state["done"] = completed >= total_size

    def finish():
        bar = state["bar"]
        if bar is not None:
            # dirty=True leaves the bar where it stopped instead of jumping to 100%
            bar.finish(dirty=not state["done"])

    return ProgressInfo(callback=callback, finish=finish)
