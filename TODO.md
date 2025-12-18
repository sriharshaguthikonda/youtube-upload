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

7. [ ] **OAuth libs update** - migrate from `oauth2client` to `google-auth`/`google-auth-oauthlib`; update console and browser flows.

8. [ ] **API error handling** - surface HttpError details, distinguish quota/permission vs retryable 5xx/429, cap exponential backoff.

9. [ ] **Dependency pinning** - add requirements/pyproject with tested versions; declare Python 3.8+ support; drop Python 2 code paths.

10.[ ] **Resumable uploads** - persist resumable upload URLs to resume after interruptions; expose `max_retries`/timeouts.

11.[ ] **Skip-if-exists robustness** - document hash/title collision limits; optionally allow skip via explicit videoId list and case-insensitive title matching.

12.[ ] **Modern README** - add quickstart (pip install, OAuth steps with screenshots), auth troubleshooting, proxy/env vars, split-video recipe, FAQ.

13.[ ] **CLI help polish** - add `--version`, group flags, richer `--help` examples, exit codes table.

14.[x] **GUI improvements** - add per-file progress bar, 

15.[x] have a log/status pane, 

16.[x] add cancel button to stop the upload, 

17.[x] able to drag-drop for files,folders , 

18.[x] field validation, 

19.[x] persist window size save and reload!. 

20.[ ] **CLI ergonomics** - auto-use `.txt` next to video as description, document title sanitization, warn when `--publish-at` lacks timezone.

21.[ ] **UTF-8 everywhere** - remove legacy decode branches; ensure Windows path handling and UTF-8 encoding consistency.

22.[ ] **Account UX** - list/select existing profiles in accounts dir; auto-create dir; document per-account `client_secrets.json` lookup.

23.[ ] **Playlist management** - create playlist once per run and reuse ID; clearer errors when playlist creation fails.

24.[ ] **Thumbnail validation** - check file exists/format (JPEG/PNG); support MIME sniffing; clarify `--thumbnail`/`--no-thumbnail`.

25.[ ] **Publish helpers** - add `--embargo-duration` to compute publishAt; warn if `privacy=public` with future publish date.

26.[ ] **Metadata templates** - allow JSON/YAML templates for title/description/tags with per-file overrides.

27.[ ] **Split helper** - add Python wrapper for ffmpeg splits (size/time-based) replacing shell-only script.

28.[ ] **Automated tests** - add unit tests for option parsing, title sanitize, hash tagging, skip-if-exists, playlist flow (mocked API).

29.[ ] **Integration smoke** - use recorded API doubles to run CI without network.

30.[ ] **Lint/format** - add ruff/flake8 + black/isort and enforce in CI.

31.[ ] **Type hints** - incrementally annotate public functions; optional mypy run.

32.[ ] **Packaging** - add `pyproject.toml`, trove classifiers, console_scripts entry point, extras (`gui`, `dev`).

33.[ ] **Binary guidance** - document pipx/venv install; add PyInstaller recipe for GUI users.

34.[ ] **Changelog & releases** - maintain CHANGELOG.md; GitHub Actions for tests/release; version bump automation.

35.[ ] **Docker refresh** - base on modern Python image, smaller layers, non-root, volume for accounts.

36.[ ] **Secret hygiene** - ensure client_secrets not bundled; add `.gitignore` entries; guidance on storing OAuth files securely.

37.[ ] **Logging hygiene** - avoid printing tokens; add `--verbose/--debug`; redact auth URLs after first step.

38.[ ] **Remove Python 2 shims** - drop `raw_input` and legacy encoding branches.

39.[ ] **GUI error surfacing** - catch SystemExit/Exceptions and show user-friendly dialog; log trace to file.

40.[ ] **GUI progress feedback** - wire CLI progress callbacks to GUI progress bar per file.

41.[ ] **Exit codes doc** - ensure nonzero exit on failure; document EXIT_CODES mapping.

42.[ ] **Proxy support** - document env vars; pass through to HTTP client; add connectivity check and clear error message.

43.[ ] **i18n readiness** - keep UI strings centralizable for future localization (minimal scaffolding).

