import argparse
import sys
from dotenv import load_dotenv

# Load environment variables from .env before anything else uses them
load_dotenv()

from agent.graph import review_app
from agent.state import ReviewState
from tools.git_tools import cleanup_temp_dirs


def run_review(repo_full_name: str, pr_number: int) -> None:
    initial_state: ReviewState = {
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
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
    except Exception as e:
        print(f"Error running review: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()