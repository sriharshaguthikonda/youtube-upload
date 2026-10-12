#!/usr/bin/env python
"""Run with ``python -m youtube_upload`` or ``python youtube_upload/__main__.py``."""
import sys

if __package__ in (None, "") and not hasattr(sys, "frozen"):
    # direct call of __main__.py: make the package importable
    import os.path
    path = os.path.realpath(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(os.path.dirname(path)))

import youtube_upload.main  # noqa: E402

if __name__ == '__main__':
    youtube_upload.main.run()
