# youtube-upload

Upload videos to YouTube from the **command line** or a small **desktop window (GUI)**,
with **several Google accounts / channels** side by side. Works on Windows, macOS and Linux.

* Resumes interrupted uploads (even after a crash or reboot) instead of starting over.
* Will not upload the same file twice to the same channel (when asked to).
* Tells the truth: the exit status is `0` only if every requested upload worked.
* Sign-in uses Google's current browser flow. Tokens are stored per account, outside the source tree.

> **Status of this fork.** This branch is a recovery of the earlier work (CLI + GUI + multiple
> accounts). Everything was tested against a *fake* YouTube server and mocked Google sign-in;
> no real Google login, upload, or Windows desktop session was exercised by the author. See
> [`docs/HANDOFF.md`](docs/HANDOFF.md) for exactly what was and was not verified.

## Contents

1. [Install](#install) · 2. [Google setup (once)](#google-setup-once) · 3. [Sign in](#sign-in)
4. [Upload](#upload) · 5. [Several accounts](#several-accounts) · 6. [The GUI](#the-gui)
7. [Duplicates, resuming, quota](#duplicates-resuming-and-quota) · 8. [Exit codes](#exit-codes)
9. [Where files are stored](#where-files-are-stored) · 10. [Upgrading from an older version](#upgrading-from-an-older-version)
11. [Troubleshooting](#troubleshooting) · 12. [Security](#security) · 13. [Development](#development)

## Install

Needs **Python 3.9 or newer** (3.11+ recommended: Python 3.9 is past end-of-life and Google's
libraries print a warning on it). Check with `python --version` (Windows: `py -3 --version`).

```bash
git clone https://github.com/sriharshaguthikonda/youtube-upload.git
cd youtube-upload
python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate
python -m pip install ".[gui]"        # ".[gui]" adds optional drag-and-drop; plain "." works too
```

Check it:

```bash
youtube-upload --version
youtube-upload --help
```

Windows launchers (no install needed beyond the steps above):

* `Youtube_upload_app.bat` starts the **GUI**. It looks for `.venv`, `venv`, the legacy
  `youtube-upload` venv, the `py` launcher, then `python` on `PATH`. If dependencies are missing it
  prints the exact `pip install` command instead of silently doing nothing.
* `bin\youtube-upload.bat` runs the **command line** from a source checkout.

## Google setup (once)

YouTube uploads need your own Google OAuth *client* (this project ships none; the old shared one
was revoked by Google).

1. Open <https://console.cloud.google.com/>, create a project.
2. **APIs & Services → Library →** enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen:** user type *External*; add the Google accounts you
   will upload with as **test users**.
   *While the app is in "Testing", Google expires sign-ins after **7 days**; "publish" the app
   (it can stay unverified for personal use) to avoid weekly re-sign-in.*
4. **APIs & Services → Credentials → Create credentials → OAuth client ID →** Application type
   **Desktop app** → create → **Download JSON**.
5. Save that file as `client_secrets.json` in one of these places (first match wins):
   * the accounts folder you pass with `--accounts-dir` (or choose in the GUI);
   * the per-user folder shown by `youtube-upload --help` / [Where files are stored](#where-files-are-stored);
   * your home folder (`~/client_secrets.json`);
   * or anywhere, passing `--client-secrets PATH`.

An example with placeholders is in [`examples/client_secrets.example.json`](examples/client_secrets.example.json).
**Never commit the real file** (it is git-ignored, and a test fails if one is tracked).

**Quota.** New projects get 10,000 API units/day and one upload costs about 1,600, so roughly six
uploads a day until you request more. When it runs out the tool stops cleanly with a clear
message. Also be aware that YouTube may keep videos uploaded through API projects that have not
passed YouTube's API compliance review **private**; check YouTube's current documentation.

## Sign in

The first upload opens your browser to sign in. You can also do it up front:

```bash
youtube-upload --login                       # default account
youtube-upload --login --account work        # a named account
```

* It listens on `127.0.0.1` for the browser's redirect (PKCE, random port, `state` checked).
* No browser on this machine (SSH, container)? Use `--no-browser`: the address is printed, open it
  on a machine with a browser. The redirect must reach this computer, so for a remote host use an
  SSH port forward together with `--auth-port` (see `--help`).
* **Sign out:** `youtube-upload --logout --account work` revokes the token at Google (best
  effort; it says if it could not) and deletes the saved sign-in. `--no-revoke` only deletes the file.
* The sign-in is refreshed automatically. If Google no longer accepts it (expired, revoked, the
  OAuth client was rotated) you get a plain message and, in interactive use, the browser opens again.

## Upload

```bash
youtube-upload --title "Beethoven" --description "Anne Sophie Mutter" \
               --category Music --tags "mutter, beethoven" --privacy unlisted \
               mutter.mp4
```

* Standard output carries only the **video id(s)**, one per line, so it is safe to script. Progress,
  warnings and the final summary go to standard error.
* Without `--title` each file's name is used (underscores become spaces; hyphens are kept;
  anything removed or shortened is reported). With several files and one `--title`, the
  `--title-template` (default `{title} [{n}/{total}]`) makes the titles distinct.
* Next to each uploaded video a `<file>.url` internet shortcut is written.
* Supported containers: `mov mp4 mpeg mpeg1 mpeg2 mpeg4 mpg avi wmv mpegps flv 3gp 3gpp webm
  dnxhr prores cineform hevc h265`. Files are checked for a real container signature (and with
  `ffprobe`, if installed, for a video stream) **before** anything is uploaded.
* More: `--publish-at 2030-01-31T12:00:00Z` (scheduled; the video is private until then),
  `--thumbnail cover.jpg` (JPEG/PNG up to 2 MB), `--playlist "Name"` (created if missing),
  `--location latitude=51.5,longitude=-0.12`, `--recording-date`, `--default-language en`,
  `--embeddable False`, `--license creativeCommon`, `--description-file notes.txt`,
  `--chunksize BYTES` (multiple of 262144), `--max-retries N`, `--no-verify`, `--open-link`, `--debug`.
* Split a long video first with `examples/split_video_for_youtube.sh` (uses ffmpeg).
* Behind a proxy? Set `https_proxy` / `http_proxy` as usual.

## Several accounts

Each account is one credentials file named after its label.

```bash
youtube-upload --account work    video.mp4                 # one account
youtube-upload --accounts work,home --title "Launch" teaser.mp4   # the same files to both
youtube-upload --list-accounts                              # what is signed in
youtube-upload --accounts-dir D:/yt --account clientA video.mp4
```

* Account folder: `--accounts-dir`, else `~/.youtube-upload-accounts` if it already exists, else
  the per-user folder's `accounts`. Credentials: `<folder>/<label>.json`; client secrets:
  `<folder>/client_secrets.json`.
* Labels may contain letters (any language), digits, `_ - .` and inner spaces, up to 64 characters.
  Path separators, `..` and Windows reserved names are rejected.
* **Accounts are isolated.** If one account cannot sign in or is out of quota, the others still run;
  the summary lists exactly which file went where. The same file sent to two accounts is two
  uploads, tracked separately per channel.

## The GUI

`Youtube_upload_app.bat` (Windows), `youtube-upload-gui`, or `python -m youtube_upload.gui_app`.

* Pick files or a folder (drag and drop with the `gui` extra), fill in what you want, press **Upload**.
* **Account label / Accounts / Accounts dir / Client secrets / Credentials file** work as on the
  command line. **Sign in** and **Sign out** buttons act on the chosen account; a status line shows
  whether it is signed in.
* Each file has its own progress bar and status ("Done", "Already on channel", "Failed", …). The
  Status pane shows the log; the red **Errors** pane lists every failed file with the reason.
* **Cancel** stops after the current chunk; running the same upload again **resumes** it.
* Settings (window position, fields, accounts history) are saved per accounts folder, atomically.
  A damaged settings file is set aside as `*.corrupt-<time>` and reported, never silently replaced.
* If the window cannot start, the reason is shown in a dialog and written to `gui_crash.log` in the
  per-user folder (the windowless launcher has no console).

## Duplicates, resuming and quota

* `--skip-if-exists hash` (GUI default): skip a file whose **content** is already on the channel.
  The check uses an exact local record plus one scan of the channel's uploads playlist per run
  (about 1 quota unit per 50 videos). The old `search().list` approach was fuzzy, delayed, and cost
  100 units per call. `--skip-if-exists title` matches titles case-insensitively. Skipped files still
  get their follow-ups (thumbnail, playlist, shortcut).
* Without the option, uploading a file that was uploaded before only prints a warning.
* **Resume:** the upload session is saved after every chunk (owner-only file). Kill the program,
  lose the network, reboot: run the same command again and it continues where the server left off.
  A session is only reused for the *same* file content and metadata and expires after 5 days.
* **Retries:** only temporary failures (network errors, 408/429/5xx, rate limits) are retried, with
  jittered back-off capped at 60 s, `Retry-After` honoured, and a 15-minute limit per stretch of
  failures. Authentication, quota and request errors are never retried.
* After uploading, the video id is checked with YouTube; a video YouTube reports as
  failed/rejected is reported as a failure.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | everything requested succeeded |
| 2 | bad options or input files |
| 3 | request/API error or quota exhausted (also: retries exhausted) |
| 4 | authentication failed (sign in needed) |
| 5 | not implemented |
| 6 | cancelled |
| 7 | partial: something succeeded and something failed |
| 130 | interrupted (Ctrl+C) |

## Where files are stored

| What | Where |
| --- | --- |
| Per-user folder | Windows `%APPDATA%\youtube-upload`; Linux/macOS `$XDG_CONFIG_HOME/youtube-upload` or `~/.config/youtube-upload`; override with `YOUTUBE_UPLOAD_HOME` |
| Default account's sign-in | `<per-user folder>/credentials.json` |
| Named accounts | `<accounts folder>/<label>.json` |
| Upload record (per channel) | `<per-user folder>/state/ledger/<channelId>.json` |
| Unfinished upload sessions | `<per-user folder>/state/sessions/*.json` (safe to delete; the upload then restarts) |
| Debug log (`--debug`) / GUI crash log | `<per-user folder>/youtube_upload_debug.log`, `gui_crash.log` |

Nothing sensitive is written inside the source tree, and token files are created readable by you only.

## Upgrading from an older version

* **Saved sign-ins keep working.** Files written by the old `oauth2client`-based versions
  (`~/.youtube-upload-credentials.json`, `~/.youtube-upload-accounts/<label>.json`, the per-account
  folders used by the GUI) are read and rewritten in the new format on first use. Secret values are
  never printed. A default sign-in found at an old location is *copied* to the per-user folder; the
  old file is left in place so you can delete it yourself.
* A `client_secrets.json` inside the source tree still works but prints a warning: move it to one of
  the places listed above.
* `--auth-browser` is ignored (the system browser is always used); the old Qt/GTK web-view backends
  and the copy-and-paste verification code are gone. The GUI checkbox "Authenticate with browser
  (GUI)" became "Open the browser automatically to sign in".
* The CLI now exits non-zero when anything fails, and `python -m youtube_upload` returns proper exit
  codes instead of a traceback.
* **If tokens or client secrets were ever committed or shared, treat them as compromised** and follow
  [`SECURITY.md`](SECURITY.md): rotate the OAuth client, revoke the tokens, then clean history. The
  agent that prepared this branch could not do any of that for you.

## Troubleshooting

| Message | Meaning / fix |
| --- | --- |
| `Google no longer accepts the saved sign-in …` | Expired (7-day "Testing" limit), revoked, or password changed → `youtube-upload --login --account NAME`. |
| `does not accept the OAuth client … (rotated)` | The client in `client_secrets.json` was deleted or its secret changed → download the current file, sign in again. |
| `different OAuth client than …` | The saved sign-in belongs to another client (after rotation) → sign in again. |
| browser says *access blocked / app not verified* | Add the account as a **test user** on the consent screen, or publish the app. |
| `quotaExceeded` / `uploadLimitExceeded` | Daily API quota or YouTube's per-channel upload limit is used up; wait or request more quota. |
| `no YouTube channel` | The Google account has no channel yet; create one on youtube.com. |
| `ffprobe found no usable video` | The file's header looks right but there is no video stream (truncated or damaged). Set `YOUTUBE_UPLOAD_NO_FFPROBE=1` to skip that check. |
| GUI does not appear on Windows | Run `Youtube_upload_app.bat` from a prompt to see the message, or read `gui_crash.log`. |

## Security

See [`SECURITY.md`](SECURITY.md) for the credential-exposure findings, the owner-only revocation
checklist and the history-purge plan. Do not paste tokens, codes or secret files into issues.

## Development

```bash
python -m pip install -e ".[dev,gui]"
python -m pytest                      # about 380 tests, ~15 s, no network
xvfb-run -a python -m pytest          # Linux/CI: lets the Tk GUI tests run headless
```

* `tests/fake_youtube.py` is a fake YouTube **HTTP server** placed under the real
  `google-api-python-client` (real request building and resumable protocol) with fault injection
  (503/429, connection resets, a killed process). It also hashes the bytes it receives.
* Real media for the container checks is generated with `ffmpeg` when available.
* `.github/workflows/tests.yml` runs the suite on Ubuntu and **Windows**, Python 3.9-3.13, builds the
  wheel, and smoke-tests the CLI after a fresh install.
* Tested dependency versions are recorded in `requirements-tested.txt`.

## Docker

```bash
docker build -t youtube-upload .
# sign in on a machine with a browser first, then mount its config folder:
docker run --rm -v "$PWD/cfg:/config" -v "$PWD:/data" youtube-upload --account work video.mp4
```

## License

GNU GPL v3. Original project by Arnau Sanchez ([tokland/youtube-upload](https://github.com/tokland/youtube-upload)).
