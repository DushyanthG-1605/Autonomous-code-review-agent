import os
from typing import List, Dict, Any
from github import Github, Auth


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

    # GitHub's create_review expects comments referencing the latest commit
    commit = pr.get_commits().reversed[0]

    formatted_comments = [
        {"path": c["path"], "line": c["line"], "body": c["body"]}
        for c in inline_comments
    ]

    pr.create_review(
        commit=commit,
        body=summary_body,
        event=event,
        comments=formatted_comments,
    )