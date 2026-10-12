"""Guard rails so credentials and tokens can never be committed again (#1, #2).

These tests only look at *names* and *shapes* of tracked files; they never
print matched content, so a failure cannot leak a secret into CI logs.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(),
    reason="needs a git checkout",
)


def _git(*args):
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=False,
    )


def _tracked():
    out = _git("ls-files", "-z").stdout
    return [p for p in out.split("\0") if p]


FORBIDDEN_NAME = re.compile(
    r"(^|/)("
    r"client_secrets?[^/]*\.json"
    r"|[^/]*credentials[^/]*\.json"
    r"|[^/]*token[^/]*\.json"
    r"|\.youtube[-_]upload[^/]*"
    r"|[^/]*\.(pem|p12|pfx|key)"
    r"|\.env(\..*)?"
    r"|gui_settings\.json"
    r")$",
    re.IGNORECASE,
)
ALLOWED_NAMES = {"examples/client_secrets.example.json"}
ACCOUNT_DIR = re.compile(r"(^|/)[^/]*_accounts?/", re.IGNORECASE)

# Shapes of real Google secrets. Written so this file does not match itself.
SECRET_SHAPES = [
    re.compile(r"GOCSPX-[A-Za-z0-9_-]{20,}"),           # OAuth client secret
    re.compile(r"ya29\.[A-Za-z0-9_-]{30,}"),            # access token
    re.compile(r"1//0[A-Za-z0-9_-]{30,}"),              # refresh token
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),               # API key
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r'"(refresh_token|client_secret|access_token)"\s*:\s*"(?!(REPLACE|YOUR|<|\s*"))[^"]{8,}"', re.IGNORECASE),
]


def test_no_tracked_credential_files():
    bad = [
        p for p in _tracked()
        if p not in ALLOWED_NAMES and (FORBIDDEN_NAME.search(p) or ACCOUNT_DIR.search(p))
    ]
    assert not bad, "credential-like files are tracked: %s" % bad


def test_no_secret_shaped_content_in_tracked_files():
    offenders = []
    for rel in _tracked():
        path = REPO / rel
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            continue
        for shape in SECRET_SHAPES:
            if shape.search(text):
                offenders.append(rel)  # name only, never the match
                break
    assert not offenders, "secret-shaped content in: %s" % offenders


@pytest.mark.parametrize("path", [
    "client_secrets.json",
    "client_secret_1234-abc.apps.googleusercontent.com.json",
    "youtube_upload/client_secrets.json",
    "youtube_upload/General_account/client_secrets.json",
    "youtube_upload/General_account/default.json",
    "youtube_upload/Electrronics_account/Electrronics_account.json",
    "youtube_upload/Social_account/client_secrets.json",
    ".youtube-upload-credentials.json",
    "youtube_upload/.youtube-upload-credentials.json",
    "youtube_upload/General_account/.youtube-upload-credentials.json",
    "work.credentials.json",
    "token.json",
    "gui_settings.json",
    "accounts/work.json",
    ".youtube-upload-accounts/work.json",
    ".env",
])
def test_gitignore_blocks_credential_paths(path):
    result = _git("check-ignore", "-q", "--no-index", path)
    assert result.returncode == 0, "%s is not ignored" % path


@pytest.mark.parametrize("path", [
    "examples/client_secrets.example.json",
    "pyproject.toml",
    "youtube_upload/main.py",
    "tests/test_repo_hygiene.py",
    "SECURITY.md",
])
def test_gitignore_does_not_hide_source(path):
    result = _git("check-ignore", "-q", "--no-index", path)
    assert result.returncode == 1, "%s must not be ignored" % path


def test_dockerignore_excludes_credentials():
    text = (REPO / ".dockerignore").read_text(encoding="utf-8")
    for needle in ("client_secret", "credentials", ".git"):
        assert needle in text


def test_example_secrets_file_is_placeholder_only():
    import json
    data = json.loads((REPO / "examples/client_secrets.example.json").read_text(encoding="utf-8"))
    flat = json.dumps(data)
    assert "REPLACE" in flat
    assert not any(shape.search(flat) for shape in SECRET_SHAPES)
