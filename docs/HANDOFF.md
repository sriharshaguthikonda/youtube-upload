# Recovery handoff

Written for the next engineer or agent. Everything here is a statement of fact about the state of
branch `claude/charming-ride-asq0ee` (base: `master` @ `6a30b55`). Epic: issue #16.

> **Not merged. No history rewritten. No force-push. No credential rotated.** Those need the owner.

## 1. Resume in five minutes

```bash
git clone https://github.com/sriharshaguthikonda/youtube-upload && cd youtube-upload
git checkout claude/charming-ride-asq0ee
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,gui]"
python -m pytest -q                                  # ~15 s, no network, no account
xvfb-run -a python -m pytest -q                      # Linux: also runs the Tk GUI tests
```

Interpreters used locally: `uv python install 3.9 3.10 3.14` (3.11–3.13 from the OS). The suite also
passes on the *oldest* allowed dependency versions:
`uv pip install --resolution lowest-direct -e ".[dev]"` on 3.9.

## 2. Which code is the real application

| Branch | Verdict |
| --- | --- |
| `master` | CLI only. No GUI, no validation. Old OOB OAuth. |
| `GUI` | early Tk wrapper; subset of the next one. |
| `multiple-youtube-accounts` | GUI + `--account/--accounts` + hash verification; subset of the next one. |
| **`shortcut-link-creation` @ `a2940ef`** | **canonical.** Superset: DnD, themes, validation, playlist prompts, shortcuts, launcher. Re-hashed history, so `git merge` conflicts; its tree is imported as `ac0db64`. |
| `auth-gui`, `modularize`, `more-retry-exceptions` | old; the last three still **track a root `client_secrets.json`** (see `SECURITY.md`). Untouched. |
| `repo-intel-artifacts`, `fix_293` | metadata / identical to master. |

Only feature knowingly dropped by the canonical tree: `_verify_uploaded_hash` (from
`multiple-youtube-accounts`). Reinstated as video-id verification + hash-tag read-back warning.

## 3. Issue matrix

Evidence: "before" reproduction, the fix commit, and the test that guards it.

| # | Status | Commit | Guard |
| --- | --- | --- | --- |
| 1, 2 secrets | **code side done; owner actions open** | `6026188` | `tests/test_repo_hygiene.py` (25) |
| 3 OAuth | done (mocked; **no live login**) | `c76a35e` | `test_auth.py`, `test_loopback.py` |
| 4 distutils | done; installs 3.9–3.14, CI fresh-install green on Linux+Windows | `1c56c4a` | CI jobs |
| 5 progressbar | done | `6c57442` | `test_cli.py::test_cli_progress_bar_*` |
| 6 swallowed errors | done | `6c57442` | `test_cli.py`, `test_uploader.py` |
| 7 resume | done (contract tested against the real client) | `6c57442` | `test_uploader.py::test_killed_process_*` |
| 8 duplicates | done | `6c57442` | ledger/remote/pagination tests |
| 9 isolation + exit code | done | `6c57442` | `test_cli.py` |
| 10 launcher | **written, not run on Windows** | `1c56c4a` | none possible here (manual gate) |
| 11 `.m3u` | done | `6c57442` | uploader test |
| 12 titles | done | `6c57442` | `test_titles_and_state.py` |
| 13 GUI errors | done; GUI tests pass on Linux/Xvfb and (39 of 41) on the Windows CI runner | `fbfee9b` | `test_gui.py` (41) |
| 14 validation | done | `6c57442` | `test_validation.py` |
| 15 retries | done | `c76a35e` | `test_retry.py` (64) |

Additional defects found and fixed (not in the original list) are itemised in the commit messages
and in the issue #16 comments.

## 4. What was verified, and how

* **Fake YouTube HTTP server under the real `googleapiclient`** (`tests/fake_youtube.py`): real
  discovery document, request building and resumable protocol; injects 503/429, connection resets,
  a killed process; hashes the bytes it receives (resumed uploads are byte-identical).
* **Mutation checks** (done by hand): disabling session loading, the ledger, or retries makes the
  matching tests fail.
* **Real Tk window** driven under Xvfb on Python 3.9 (Tk 8.6) and 3.14 (Tk 9.0), including a
  "no widget is touched off the main thread" guard and screenshots reviewed by eye.
* **Oldest allowed dependency versions** pass; this caught PKCE not being enabled by default.
* **GitHub Actions** (ubuntu + windows-latest × Python 3.9–3.13): fresh `pip install` and
  `youtube-upload --help` / `python -m youtube_upload --help` pass; the suite passes on Windows,
  **including the Tk GUI tests** (run 3 on windows-latest/3.13: 381 passed, 7 skipped; in
  `test_gui.py` 39 passed, 1 skipped by design (needs a headless Linux), 1 skipped with a transient
  `Can't find a usable init.tcl` raised by `tk.Tk()` in a random test - the fixture now retries).
  That is a headless CI desktop, not a human using the window.

* **Second review pass (findings A–P)**: every finding was reproduced by a failing test first; the
  fixes are in `f0b6e5b` (auth/loopback/redaction), `f8cb9ed` (GUI) and `982f857` (upload core). Guards:
  `tests/test_review_findings.py` (mutation-checked: the session-recording and single-chunk tests fail
  when the fix is removed) and `tests/test_crash_recovery_fuzz.py` (40 seeded crash/fault scenarios incl.
  single-chunk files and random Cancel, 10 multi-file scenarios, 20 unwritable-state-folder scenarios).
  Note: the fuzz alone does **not** catch removal of the early session record, because skip mode has a
  second safety net (remote scan); the dedicated tests do.
* Local results on this commit: 485 passed / 2 skipped (3.12, no Tk); 542 passed / 1 skipped (3.9 and
  3.14 with Tk under Xvfb, and 3.9 on the lowest allowed dependency versions). Skips: "needs a non-root
  POSIX user" and (3.12 only) no `tkinter`.

## 5. NOT verified — do not claim otherwise

1. **No real Google sign-in, no real upload.** Google's live behaviour is unobserved: whether the
   69-character `hash:<sha256>` tag is accepted by the API (a 400 `invalidTags` would fail every
   upload with `--skip-if-exists hash`; the tag is the pre-existing design, kept for compatibility;
   the uploader retries once without it on `invalidTags`/`invalidVideoKeywords`, tested only against the fake); API-audit "private only" restrictions; real quota numbers.
2. **Windows GUI with a human**: only the headless CI desktop has run the Tk tests.
   `Youtube_upload_app.bat` has never been executed.
3. **macOS** untested.
4. Behaviour with `tkinterdnd2` installed (drag and drop) is untested.
5. Very large files (multi-GB) and slow links: logic is covered, wall-clock behaviour is not.
6. Locking on network file systems (NFS/SMB) is unobserved; `flock` semantics vary.
7. Whether Google accepts omitting `categoryId` and the cleaned description/tags exactly as the
   uploader now sends them is covered only by the fake server.
8. GUI: the upload worker is a daemon thread, so closing the window mid-upload stops it without a
   final status (the next run resumes). The chunk size is not exposed in the GUI.

## 6. Owner-only actions (the agent could not and did not do these)

See `SECURITY.md` §3–4. In short: rotate the OAuth client named in issues #1/#2, revoke tokens at
<https://myaccount.google.com/permissions>, delete or purge `auth-gui` / `modularize` /
`more-retry-exceptions` (root `client_secrets.json`), ask GitHub support to drop cached views of the
pre-2025-12-16 commits, enable secret scanning and push protection, decide whether to publish the
OAuth consent screen (7-day token expiry in "Testing").

## 7. Design notes worth knowing

* State lives under the per-user folder (`paths.app_home()`), never in the source tree. Ledger is
  keyed by **channel id**; sessions by (channel, file sha256, size) and bound to a digest of the
  request body.
* `retry.Retrier` is the only place that sleeps/retries; `upload_video.upload` resumes via public
  attributes (`resumable_uri`, `resumable_progress`) after its own status query. If a future
  `google-api-python-client` changes `next_chunk`, `tests/test_uploader.py` (real client) fails.
* The resumable-session URI is persisted the instant Google issues it (`_SessionRecordingHttp` in
  `upload_video.py`), so even a single-request file that crashes after the server committed can
  resume. If the state folder is unwritable (disk full, antivirus), the upload still completes; only
  resumability is lost, and `--skip-if-exists hash` still prevents a duplicate.
* `ProcessLock` (flock / `msvcrt.locking`) guards the ledger and an in-flight lock per
  (channel, file hash). On file systems that cannot lock (some NFS/SMB mounts) it degrades to a
  no-op and says so; two simultaneous runs on such a share are then not excluded.
* The `hash:<sha256>` tag is public metadata on the video.
* GUI: worker thread posts closures to a queue; never call a widget from the worker.
* Exit codes: 0 ok, 2 options, 3 request/quota, 4 auth, 6 cancelled, 7 partial, 130 interrupted.

## 8. Next tasks, in priority order

1. Owner security actions (above) — nothing else matters if the secrets are live.
2. Run the GUI tests on a developer's Windows machine:
   `pip install -e ".[dev,gui]" && python -m pytest tests/test_gui.py -rs`, then use the window.
3. One supervised **live** smoke test by the owner with a throw-away channel: `--login`, upload a
   5-second private video, interrupt it, resume it, `--skip-if-exists hash` it again, `--logout`.
   The uploader already retries once without the `hash:` tag if the API answers `invalidTags` (and says
   so); if you see that warning, consider dropping the tag for good and relying on the ledger.
4. Run `Youtube_upload_app.bat` on a clean Windows machine.
5. Optional: expose `--chunksize` in the GUI; `--dry-run`; per-account ledger export.
