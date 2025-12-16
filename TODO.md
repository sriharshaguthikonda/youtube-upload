1.[ ] **post upload verification** - after uploading the video, verify via content hash/tag search that the video exists on the channel.

2.[ ] **playlist verification** - if the folder name matches an existing playlist, add the upload there; create if missing.

3.[ ] **shortcut creation** - in the video directory, create an internet shortcut to the uploaded video URL with the same base name as the video file.

4.[ ] **OAuth libs update** - migrate from `oauth2client` to `google-auth`/`google-auth-oauthlib`; update console and browser flows.

5.[ ] **API error handling** - surface HttpError details, distinguish quota/permission vs retryable 5xx/429, cap exponential backoff.

6.[ ] **Dependency pinning** - add requirements/pyproject with tested versions; declare Python 3.8+ support; drop Python 2 code paths.

7.[ ] **Resumable uploads** - persist resumable upload URLs to resume after interruptions; expose `max_retries`/timeouts.

8.[ ] **Skip-if-exists robustness** - document hash/title collision limits; optionally allow skip via explicit videoId list and case-insensitive title matching.

9.[ ] **Modern README** - add quickstart (pip install, OAuth steps with screenshots), auth troubleshooting, proxy/env vars, split-video recipe, FAQ.

10.[ ] **CLI help polish** - add `--version`, group flags, richer `--help` examples, exit codes table.

11.[ ] **GUI improvements** - add per-file progress bar, log/status pane, cancel button, drag-drop, field validation, persist window size/last folder.

12.[ ] **CLI ergonomics** - auto-use `.txt` next to video as description, document title sanitization, warn when `--publish-at` lacks timezone.

13.[ ] **UTF-8 everywhere** - remove legacy decode branches; ensure Windows path handling and UTF-8 encoding consistency.

14.[ ] **Account UX** - list/select existing profiles in accounts dir; auto-create dir; document per-account `client_secrets.json` lookup.

15.[ ] **Playlist management** - create playlist once per run and reuse ID; clearer errors when playlist creation fails.

16.[ ] **Thumbnail validation** - check file exists/format (JPEG/PNG); support MIME sniffing; clarify `--thumbnail`/`--no-thumbnail`.

17.[ ] **Publish helpers** - add `--embargo-duration` to compute publishAt; warn if `privacy=public` with future publish date.

18.[ ] **Metadata templates** - allow JSON/YAML templates for title/description/tags with per-file overrides.

19.[ ] **Split helper** - add Python wrapper for ffmpeg splits (size/time-based) replacing shell-only script.

20.[ ] **Automated tests** - add unit tests for option parsing, title sanitize, hash tagging, skip-if-exists, playlist flow (mocked API).

21.[ ] **Integration smoke** - use recorded API doubles to run CI without network.

22.[ ] **Lint/format** - add ruff/flake8 + black/isort and enforce in CI.

23.[ ] **Type hints** - incrementally annotate public functions; optional mypy run.

24.[ ] **Packaging** - add `pyproject.toml`, trove classifiers, console_scripts entry point, extras (`gui`, `dev`).

25.[ ] **Binary guidance** - document pipx/venv install; add PyInstaller recipe for GUI users.

26.[ ] **Changelog & releases** - maintain CHANGELOG.md; GitHub Actions for tests/release; version bump automation.

27.[ ] **Docker refresh** - base on modern Python image, smaller layers, non-root, volume for accounts.

28.[ ] **Secret hygiene** - ensure client_secrets not bundled; add `.gitignore` entries; guidance on storing OAuth files securely.

29.[ ] **Logging hygiene** - avoid printing tokens; add `--verbose/--debug`; redact auth URLs after first step.

30.[ ] **Remove Python 2 shims** - drop `raw_input` and legacy encoding branches.

31.[ ] **GUI error surfacing** - catch SystemExit/Exceptions and show user-friendly dialog; log trace to file.

32.[ ] **GUI progress feedback** - wire CLI progress callbacks to GUI progress bar per file.

33.[ ] **Exit codes doc** - ensure nonzero exit on failure; document EXIT_CODES mapping.

34.[ ] **Proxy support** - document env vars; pass through to HTTP client; add connectivity check and clear error message.

35.[ ] **i18n readiness** - keep UI strings centralizable for future localization (minimal scaffolding).
