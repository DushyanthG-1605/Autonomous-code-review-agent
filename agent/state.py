from typing import TypedDict, List, Dict, Any, Optional


class ReviewFinding(TypedDict):
    file: str
    line: int
    severity: str      # "critical" | "major" | "minor" | "nit"
    category: str       # "bug" | "security" | "style" | "maintainability"
    comment: str


class ReviewState(TypedDict):
    # Inputs
    repo_full_name: str          # e.g. "DushyanthG-1605/Autonomous-code-review-agent"
    pr_number: int
    execution_id: str

    # Populated by fetch_diff
    pr_files: List[Dict[str, Any]]      # filename, patch, status, raw content
    pr_author: str                      # GitHub login of the PR author (used as the TRAC actor)

    # Populated by retrieve_context
    repo_local_path: str                      # temp checkout of the PR head
    retrieved_context: Dict[str, List[str]]   # filename -> list of related code snippets

    # Populated by static_analysis
    lint_findings: List[Dict[str, Any]]

    # Populated by llm_review
    review_comments: List[ReviewFinding]
    review_notes: List[str]

    # Populated by prioritize
    prioritized_comments: List[ReviewFinding]
    has_critical: bool

    # Populated by post_comments
    final_summary: str
    posted: bool