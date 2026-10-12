# Security notes and credential remediation

This document records what is known about the credential exposure reported in
issues #1 and #2, what the code now does to prevent a repeat, and what only the
repository / Google account **owner** can do. No secret value appears here,
and none should ever be pasted into an issue, PR or log.

## 1. What was found (redacted, verified 2026-10-13)

All findings below come from file *names*, counts and commit hashes. Secret
values were never read into logs or notes.

| Item | Result |
| --- | --- |
| Paths named in #1/#2 (`youtube_upload/{General,Social,Electrronics}_account/client_secrets.json`, `.../default.json`, `.../Electrronics_account.json`) | **Not present in any fetched ref or its history** (all 9 remote branches, 274 commits). |
| The client ID project number quoted in #1 | **Not present in any fetched ref or history.** |
| Root `client_secrets.json` | Tracked in history (introduced `e70474d` 2015-01-13 and `0228617` 2015-07-23, deleted `7358bdf`/`dfee7d6` 2018-08-29) **and still tracked at the tip of three branches**: `auth-gui`, `modularize`, `more-retry-exceptions`. 522 bytes, an "installed" client with a client secret. It is the upstream project's old default client, which the README says Google revoked. |
| Refresh/access tokens (`refresh_token`, `access_token`, `ya29.`, `1//0…`) in any fetched ref | **None found.** |
| `shortcut-link-creation` also tracked | `youtube_upload/General_account/.youtube_upload_gui_settings.json` (local paths only, no tokens) and an 860 KB saved web page. Both removed from this snapshot. |
| `.gitignore` | Line 66 was corrupted (two patterns fused: `*.code-workspaceyoutube_upload/General_account/...`), and the rules had real gaps: the exact file name Google gives you when you download an OAuth client (`client_secret_<id>.apps.googleusercontent.com.json`), accounts folders outside `youtube_upload/*/`, `token*.json` and `.env` were **not** ignored (verified with `git check-ignore` against the old file). Rewritten and covered by tests. |
| `Dockerfile` | `COPY . /data` would have baked any local credential file into the image. `.dockerignore` added. |

### What this means

* Issues #1 and #2 describe **files that are not reachable from any current
  ref**. The most likely explanation is the history rewrite on 2025-12-16
  (`preserve history rewrite (remove secrets)`). Rewriting does **not** remove
  the old commits from GitHub: they stay fetchable by SHA, through forks,
  clones, caches and pull-request refs until GitHub garbage-collects them.
* Therefore **treat every credential named in #1 and #2 as compromised** until
  you have rotated and revoked it. A clean-looking history is not evidence of
  safety.
* The old root `client_secrets.json` on the three branches is a separate,
  confirmed exposure (low impact if the client is already revoked, but verify).

## 2. What the code now does

* No secret, token, per-account folder or GUI settings file is tracked.
  `tests/test_repo_hygiene.py` fails the build if one is (by name, by shape of
  Google token/secret strings, and by `git check-ignore` on the usual paths).
* `.gitignore` and `.dockerignore` cover client secrets (including Google's
  `client_secret_<id>.apps.googleusercontent.com.json` download name),
  credentials, token files, account folders, GUI settings and keys.
* Tokens are stored **outside the source tree** by default, under the per-user
  application-data directory (`%APPDATA%\youtube-upload` on Windows,
  `$XDG_CONFIG_HOME/youtube-upload` or `~/.config/youtube-upload` elsewhere;
  override with `YOUTUBE_UPLOAD_HOME`). Token files are written atomically and
  created with owner-only permissions (`0600`) on POSIX systems.
* Client secrets are never looked up inside the package directory or repo root
  by default any more; pass `--client-secrets` or put the file in the account
  folder.
* `examples/client_secrets.example.json` is the only allowed JSON of that
  shape and contains placeholders only.

## 3. Owner checklist (only you can do these)

The agent that produced this branch cannot rotate or revoke anything and has
not tried to.

1. **Rotate the OAuth client** that issues #1/#2 refer to
   (Google Cloud Console → APIs & Services → Credentials → the OAuth client).
   Create a new client secret (or a new client), update your local
   `client_secrets.json`, then disable/delete the old secret or client. Deleting
   the client invalidates every token issued to it.
2. **Revoke the user tokens** for each affected Google account at
   <https://myaccount.google.com/permissions> (remove the app) and then
   re-authorise with the new client.
3. **Check for misuse**: YouTube Studio → channel activity / recent uploads,
   Google Cloud Console → APIs & Services → Metrics and Logs for unexpected
   calls or quota use.
4. **Old root `client_secrets.json`**: confirm the client is deleted in the
   project that owns it (the upstream README says it was revoked). Then delete
   the three branches that still track it (`auth-gui`, `modularize`,
   `more-retry-exceptions`) or purge the file (below).
5. **Turn on GitHub secret scanning and push protection** for the repository
   (Settings → Code security) so a repeat is blocked at push time.
6. **Ask GitHub Support** to remove cached views and dangling objects for the
   old commits if you know their SHAs (Settings → the rewritten-history period
   around 2025-12-16). Also check for forks of the repository.

## 4. History purge plan (requires your explicit approval)

Nothing here was run, and this branch does **not** rewrite or force-push
history. Rewriting breaks every existing clone and fork, so do it deliberately:

```bash
# 1. Work on a throw-away mirror, never your working copy.
git clone --mirror https://github.com/<owner>/youtube-upload.git
cd youtube-upload.git

# 2. See exactly what would go (names only, no contents).
git log --all --oneline -- client_secrets.json
git log --all --name-only --pretty=format: | sort -u | grep -Ei 'client_secret|credential|_account/'

# 3. Rewrite (pip install git-filter-repo). Adjust the path list to what step 2 found.
git filter-repo --invert-paths \
  --path client_secrets.json \
  --path-glob '*_account/*.json' \
  --path-glob '*credentials*.json'

# 4. Only after you have rotated the secrets AND told collaborators:
git push --force --mirror
```

After the push, every collaborator must re-clone (not pull), and open forks
and pull-request refs keep the old objects unless GitHub support purges them.
Rotation in section 3 is what actually protects you; the purge is hygiene.

## 5. Reporting a vulnerability

Open a private security advisory on the repository, or email the owner. Do not
put credentials, tokens or authorisation codes in public issues.
