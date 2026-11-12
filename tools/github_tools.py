import os
import logging
import re
from typing import List, Dict, Any
from github import Github, Auth
from github.GithubException import GithubException

logger = logging.getLogger(__name__)


class ReviewPublicationError(RuntimeError):
    def __init__(
        self,
        status: int,
        message: str,
        endpoint: str,
        stage: str,
        accepted_permissions: str | None = None,
    ):
        self.status = status
        self.message = message
        self.endpoint = endpoint
        self.stage = stage
        self.accepted_permissions = accepted_permissions
        super().__init__(
            f"GitHub review {stage} failed ({status}) at POST {endpoint}: {message}"
        )


def get_github_client() -> Github:
    """Authenticate and return a PyGithub client using the token from environment."""
    token = os.environ["GITHUB_TOKEN"]
    auth = Auth.Token(token)
    return Github(auth=auth)


def fetch_pr_files(repo_full_name: str, pr_number: int) -> List[Dict[str, Any]]:
    """
    Fetch changed files for a given PR.
    Returns a list of dicts: {filename, status, patch, additions, deletions, raw_url}
    """
    gh = get_github_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    files = []
    for f in pr.get_files():
        files.append({
            "filename": f.filename,
            "status": f.status,          # "added" | "modified" | "removed"
            "patch": f.patch if f.patch else "",   # unified diff text
            "additions": f.additions,
            "deletions": f.deletions,
            "raw_url": f.raw_url,
        })
    return files


def fetch_pr_author(repo_full_name: str, pr_number: int) -> str:
    """Return the GitHub login of the user who opened the PR."""
    gh = get_github_client()
    pr = gh.get_repo(repo_full_name).get_pull(pr_number)
    return pr.user.login if pr.user else "unknown"


def fetch_file_content(repo_full_name: str, filepath: str, ref: str) -> str:
    """
    Fetch the full content of a file at a specific commit/branch.
    Used to give the LLM more context than just the diff hunk.
    """
    gh = get_github_client()
    repo = gh.get_repo(repo_full_name)
    try:
        content_file = repo.get_contents(filepath, ref=ref)
        return content_file.decoded_content.decode("utf-8", errors="replace")
    except Exception:
        return ""  # file may have been deleted, or is binary


def post_review(
    repo_full_name: str,
    pr_number: int,
    summary_body: str,
    inline_comments: List[Dict[str, Any]],
    request_changes: bool = False,
) -> None:
    """
    Post a GitHub PR review with a summary and inline comments.
    inline_comments: list of {"path": str, "line": int, "body": str}
    """
    gh = get_github_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    event = "REQUEST_CHANGES" if request_changes else "COMMENT"
    endpoint = f"/repos/{repo_full_name}/pulls/{pr_number}/reviews"

    changed_lines = {}
    for changed_file in pr.get_files():
        if changed_file.patch:
            changed_lines[changed_file.filename] = _added_line_numbers(changed_file.patch)

    formatted_comments = []
    for comment in inline_comments:
        line = comment.get("line")
        path = comment.get("path")
        if (
            not isinstance(path, str)
            or not isinstance(line, int)
            or line not in changed_lines.get(path, set())
        ):
            logger.warning(
                "Skipping invalid inline review comment for %s:%s; line is not an added diff line",
                path,
                line,
            )
            continue
        formatted_comments.append({
            "path": path,
            "line": line,
            "side": "RIGHT",
            "body": comment["body"],
        })

    # Pin review comments to the PR head commit and use GitHub's PR Reviews API.
    commit = repo.get_commit(pr.head.sha)
    try:
        pr.create_review(
            commit=commit,
            body=summary_body,
            event=event,
            comments=formatted_comments,
        )
    except GithubException as exc:
        if formatted_comments and exc.status in (400, 422):
            logger.warning(
                "GitHub rejected inline comments for %s with %s; retrying summary-only review",
                endpoint,
                exc.status,
            )
            try:
                pr.create_review(
                    commit=commit,
                    body=summary_body,
                    event=event,
                    comments=[],
                )
                return
            except GithubException as retry_exc:
                exc = retry_exc

        data = exc.data if isinstance(exc.data, dict) else {}
        message = str(data.get("message", exc))
        accepted_permissions = (exc.headers or {}).get(
            "X-Accepted-GitHub-Permissions"
        )
        logger.error(
            "GitHub review creation failed: method=POST endpoint=%s stage=create_review "
            "status=%s message=%s accepted_permissions=%s",
            endpoint,
            exc.status,
            message,
            accepted_permissions or "<not provided>",
        )
        raise ReviewPublicationError(
            status=exc.status,
            message=message,
            endpoint=endpoint,
            stage="create_review",
            accepted_permissions=accepted_permissions,
        ) from exc


def _added_line_numbers(patch: str) -> set[int]:
    added_lines = set()
    current_new_line = None
    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,\d+)?", line)
            current_new_line = int(match.group(1)) if match else None
        elif current_new_line is None or line.startswith("\\"):
            continue
        elif line.startswith("+++"):
            continue
        elif line.startswith("+"):
            added_lines.add(current_new_line)
            current_new_line += 1
        elif line.startswith("-"):
            continue
        else:
            current_new_line += 1
    return added_lines