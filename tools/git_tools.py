"""Git helpers: token-safe PR checkout and temp-dir cleanup.

The GitHub token is passed to git through environment variables (never in the
URL, the command line, or .git/config), and any git error text is redacted
before it is raised, so credentials cannot leak into logs.
"""
import base64
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import List

_TEMP_DIRS: List[str] = []


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def _basic_auth(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _redact(text: str) -> str:
    token = _token()
    if not token:
        return text
    return text.replace(_basic_auth(token), "***").replace(token, "***")


def _run_git(args: List[str], cwd: str | None = None) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    token = _token()
    if token:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {_basic_auth(token)}"

    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        # Deliberately NOT using CalledProcessError: its message includes the command.
        raise RuntimeError(f"git {args[0]} failed: {_redact(result.stderr.strip())}")
    return result.stdout


def clone_pr_head(repo_full_name: str, pr_number: int) -> str:
    """Check out the head commit of a pull request into a temp dir; return its path.

    `git clone --branch refs/pull/N/head` is rejected by git, so we init an
    empty repo, fetch the PR ref explicitly, and check out FETCH_HEAD.
    """
    path = tempfile.mkdtemp(prefix="repo_rag_")
    _TEMP_DIRS.append(path)

    url = f"https://github.com/{repo_full_name}.git"   # no credentials in the URL
    _run_git(["init", "-q"], cwd=path)
    _run_git(["remote", "add", "origin", url], cwd=path)
    _run_git(["fetch", "--depth", "1", "origin", f"pull/{pr_number}/head"], cwd=path)
    _run_git(["checkout", "-q", "FETCH_HEAD"], cwd=path)
    return path


def _on_rm_error(func, path, _exc):
    # Git object files are read-only on Windows; clear the flag and retry.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def cleanup_temp_dirs() -> None:
    """Delete every temp checkout created in this process. Safe to call repeatedly."""
    while _TEMP_DIRS:
        path = _TEMP_DIRS.pop()
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_on_rm_error)
        else:
            shutil.rmtree(path, onerror=_on_rm_error)
