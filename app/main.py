import argparse
import os
import sys
import uuid
from dotenv import load_dotenv
from github import Auth, Github, GithubException

# Load environment variables from .env before anything else uses them
load_dotenv()

from agent.graph import review_app
from agent.state import ReviewState
from tools.git_tools import cleanup_temp_dirs


def _github_error_message(operation: str, exc: GithubException) -> str:
    status = exc.status
    response = exc.data if isinstance(exc.data, dict) else {}
    detail = response.get("message", "GitHub API request failed")

    if status == 401:
        return "GitHub authentication failed (401). Check that GITHUB_TOKEN is valid."
    if status == 403 and "rate limit" in str(detail).lower():
        return "GitHub API rate limit exceeded (403). Wait for the limit to reset and retry."
    if status == 403 or status == 429:
        return (
            f"GitHub denied {operation} ({status}). Check token access and fine-grained "
            "token permissions: Contents: read and Pull requests: read/write."
        )
    if status == 404:
        return f"GitHub could not find or allow access to the requested resource during {operation} (404)."
    return f"GitHub {operation} failed ({status}): {detail}"


def _select_pull_request(repo, requested_number: int) -> int:
    try:
        pull = repo.get_pull(requested_number)
    except GithubException as exc:
        if exc.status != 404:
            raise RuntimeError(_github_error_message("pull request preflight", exc)) from exc

        try:
            pulls = list(repo.get_pulls(state="open", sort="updated", direction="desc"))
        except GithubException as list_exc:
            raise RuntimeError(
                _github_error_message("listing available pull requests", list_exc)
            ) from list_exc
        if not pulls:
            raise RuntimeError(
                f"Pull Request #{requested_number} does not exist in {repo.full_name}, "
                "and the repository has no open pull requests to select."
            )

        print(f"Pull Request #{requested_number} was not found. Available open pull requests:")
        for pull in pulls:
            print(f"  #{pull.number}: {pull.title}")

        try:
            selected = int(input("Enter an available PR number to review: ").strip())
        except (EOFError, ValueError) as input_exc:
            available = ", ".join(f"#{pull.number}" for pull in pulls)
            raise RuntimeError(
                f"Select an existing PR and rerun. Available pull requests: {available}"
            ) from input_exc

        if selected not in {pull.number for pull in pulls}:
            raise RuntimeError(f"PR #{selected} is not in the available pull request list.")
        return _select_pull_request(repo, selected)

    try:
        list(pull.get_files())
        list(pull.get_commits())
    except GithubException as exc:
        raise RuntimeError(_github_error_message("reading pull request files/commits", exc)) from exc
    return requested_number


def _verify_github_access(repo_full_name: str, pr_number: int) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is missing. Set it in .env before running a review.")

    try:
        repo = Github(auth=Auth.Token(token)).get_repo(repo_full_name)
    except GithubException as exc:
        raise RuntimeError(_github_error_message("repository access check", exc)) from exc
    return _select_pull_request(repo, pr_number)


def run_review(repo_full_name: str, pr_number: int) -> None:
    pr_number = _verify_github_access(repo_full_name, pr_number)

    initial_state: ReviewState = {
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "execution_id": str(uuid.uuid4()),
        "pr_files": [],
        "pr_author": "",
        "repo_local_path": "",
        "retrieved_context": {},
        "lint_findings": [],
        "review_comments": [],
        "review_notes": [],
        "prioritized_comments": [],
        "has_critical": False,
        "final_summary": "",
        "posted": False,
    }

    print(f"Starting review for {repo_full_name}#{pr_number}...\n")

    try:
        final_state = review_app.invoke(initial_state)
    finally:
        cleanup_temp_dirs()   # always remove temp checkouts, even if the review fails

    print("\n--- Review Complete ---")
    print(final_state["final_summary"])
    print(f"\nPosted to GitHub: {final_state['posted']}")


def main():
    parser = argparse.ArgumentParser(description="Autonomous Code Review Agent")
    parser.add_argument("repo", help="Repo in owner/name format, e.g. DushyanthG-1605/my-repo")
    parser.add_argument("pr_number", type=int, help="Pull request number to review")
    args = parser.parse_args()

    try:
        run_review(args.repo, args.pr_number)
    except GithubException as e:
        print(
            f"Error running review: {_github_error_message('review workflow', e)}",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as e:
        print(f"Error running review: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()