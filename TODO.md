1.[x] **post upload verification** - after uploading the video, verify via content hash/tag search that the video exists on the channel.


3.[x] **shortcut creation** - in the video directory, create an internet shortcut to the uploaded video URL with the same base name as the video file.



4. [ ] **validation** - before uploading, let's check if it is a valid video format that is accepted by youtube.







######## ########  ######  ######## #### ##    ##  ######   
   ##    ##       ##    ##    ##     ##  ###   ## ##    ##  
   ##    ##       ##          ##     ##  ####  ## ##        
   ##    ######    ######     ##     ##  ## ## ## ##   #### 
   ##    ##             ##    ##     ##  ##  #### ##    ##  
   ##    ##       ##    ##    ##     ##  ##   ### ##    ##  
   ##    ########  ######     ##    #### ##    ##  ######   











5. [x] ** content validation** - check the content of the file as well as in if it is an mp4 video there should be some mp4 content in the video.

6. [ ] **playlist verification** - if the folder name matches an existing playlist, add the upload there; create if missing.

7. [x] **OAuth libs update** - migrate from `oauth2client` to `google-auth`/`google-auth-oauthlib`; update console and browser flows.  _(done: OAuth libs: google-auth-oauthlib loopback flow (auth/__init__.py, auth/loopback.py))_

8. [x] **API error handling** - surface HttpError details, distinguish quota/permission vs retryable 5xx/429, cap exponential backoff.  _(done: API errors: retry.py classifies quota/auth/transient, capped back-off, Retry-After)_

9. [x] **Dependency pinning** - add requirements/pyproject with tested versions; declare Python 3.8+ support; drop Python 2 code paths.  _(done: pyproject.toml, tested ranges + requirements-tested.txt, Python >=3.9)_

10. [x] **Resumable uploads** - persist resumable upload URLs to resume after interruptions; expose `max_retries`/timeouts.  _(done: resumable sessions persisted and resumed (upload_video.py))_

11. [x] **Skip-if-exists robustness** - document hash/title collision limits; optionally allow skip via explicit videoId list and case-insensitive title matching.  _(done: skip-if-exists: case-insensitive title match, exact local ledger, uploads-playlist scan)_

12.[ ] **Modern README** - add quickstart (pip install, OAuth steps with screenshots), auth troubleshooting, proxy/env vars, split-video recipe, FAQ.

13. [x] **CLI help polish** - add `--version`, group flags, richer `--help` examples, exit codes table.  _(done: --version added; exit codes documented in README)_

14.[x] **GUI improvements** - add per-file progress bar, 

15.[x] have a log/status pane, 

16.[x] add cancel button to stop the upload, 

17.[x] able to drag-drop for files,folders , 

18.[x] field validation, 

19.[x] persist window size save and reload!. 

20.[ ] **CLI ergonomics** - auto-use `.txt` next to video as description, document title sanitization, warn when `--publish-at` lacks timezone.

21. [x] **UTF-8 everywhere** - remove legacy decode branches; ensure Windows path handling and UTF-8 encoding consistency.  _(done: UTF-8: legacy decode branches removed; console encoding errors cannot abort an upload)_

22. [x] **Account UX** - list/select existing profiles in accounts dir; auto-create dir; document per-account `client_secrets.json` lookup.  _(done: list accounts (--list-accounts), sign in/out, per-account folder lookup documented)_

23. [x] **Playlist management** - create playlist once per run and reuse ID; clearer errors when playlist creation fails.  _(done: playlist looked up/created once per run and reused)_

24. [x] **Thumbnail validation** - check file exists/format (JPEG/PNG); support MIME sniffing; clarify `--thumbnail`/`--no-thumbnail`.  _(done: thumbnail validated (exists, JPEG/PNG signature, <= 2 MB))_

25.[ ] **Publish helpers** - add `--embargo-duration` to compute publishAt; warn if `privacy=public` with future publish date.

26.[ ] **Metadata templates** - allow JSON/YAML templates for title/description/tags with per-file overrides.

27.[ ] **Split helper** - add Python wrapper for ffmpeg splits (size/time-based) replacing shell-only script.

28. [x] **Automated tests** - add unit tests for option parsing, title sanitize, hash tagging, skip-if-exists, playlist flow (mocked API).  _(done: automated tests (see tests/))_

29. [x] **Integration smoke** - use recorded API doubles to run CI without network.  _(done: fake YouTube HTTP server under the real client (tests/fake_youtube.py))_

30.[ ] **Lint/format** - add ruff/flake8 + black/isort and enforce in CI.

31.[ ] **Type hints** - incrementally annotate public functions; optional mypy run.

32. [x] **Packaging** - add `pyproject.toml`, trove classifiers, console_scripts entry point, extras (`gui`, `dev`).  _(done: pyproject.toml, console + gui entry points, gui/dev extras)_

33.[ ] **Binary guidance** - document pipx/venv install; add PyInstaller recipe for GUI users.

34.[ ] **Changelog & releases** - maintain CHANGELOG.md; GitHub Actions for tests/release; version bump automation.

35. [x] **Docker refresh** - base on modern Python image, smaller layers, non-root, volume for accounts.  _(done: Dockerfile refreshed (slim image, non-root, config volume))_

36. [x] **Secret hygiene** - ensure client_secrets not bundled; add `.gitignore` entries; guidance on storing OAuth files securely.  _(done: secret hygiene: .gitignore/.dockerignore, example file, hygiene test, SECURITY.md)_

37. [x] **Logging hygiene** - avoid printing tokens; add `--verbose/--debug`; redact auth URLs after first step.  _(done: logging hygiene: tokens/codes/session URIs never logged (tested))_

38. [x] **Remove Python 2 shims** - drop `raw_input` and legacy encoding branches.  _(done: Python 2 shims removed)_

39. [x] **GUI error surfacing** - catch SystemExit/Exceptions and show user-friendly dialog; log trace to file.  _(done: GUI errors surfaced (Errors pane, dialogs, gui_crash.log))_

40. [x] **GUI progress feedback** - wire CLI progress callbacks to GUI progress bar per file.  _(done: per-file progress bars wired to upload callbacks)_

41. [x] **Exit codes doc** - ensure nonzero exit on failure; document EXIT_CODES mapping.  _(done: non-zero exit on failure; EXIT_CODES documented)_

42.[ ] **Proxy support** - document env vars; pass through to HTTP client; add connectivity check and clear error message.

43.[ ] **i18n readiness** - keep UI strings centralizable for future localization (minimal scaffolding).

44.[x] **progressbar import crash** - `get_progress_info` references `progressbar` even when `progressbar2` is installed, leading to NameError whenever the dependency is present; alias the import so CLI progress bars work instead of crashing.

45.[x] **GUI thread safety** - upload worker and progress callbacks call `_log`/`_log_error` directly, mutating Tk widgets off the main thread and intermittently throwing Tk `RuntimeError`/hangs; route all UI updates through `root.after`.

46. [x] **Launcher path brittleness** - `Youtube_upload_app.bat` hardcodes `youtube-upload\\Scripts\\pythonw.exe`, so the GUI fails to start unless that specific venv exists; fall back to `py -m youtube_upload.gui_app` or auto-detect the interpreter.  _(done: launcher no longer hard-codes a venv path)_

