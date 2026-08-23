import os
import json
import subprocess
from typing import Dict, Any

from anthropic import Anthropic

from agent.state import ReviewState
from tools.github_tools import fetch_pr_files, fetch_file_content, post_review
from tools.rag_tools import clone_repo_at_ref, build_vector_store, retrieve_related_context
from tools.trac_reporter import report_to_trac


# ---------- Node 1: fetch_diff ----------

def fetch_diff_node(state: ReviewState) -> Dict[str, Any]:
    files = fetch_pr_files(state["repo_full_name"], state["pr_number"])
    return {"pr_files": files}


# ---------- Node 2: retrieve_context ----------

def retrieve_context_node(state: ReviewState) -> Dict[str, Any]:
    # Clone the repo at the PR's head so the vector store reflects the changed code
    repo_local_path = clone_repo_at_ref(
        state["repo_full_name"],
        ref=f"refs/pull/{state['pr_number']}/head"
    )
    vector_store = build_vector_store(repo_local_path)

    context_map: Dict[str, list] = {}
    for f in state["pr_files"]:
        # Use the patch text itself as the similarity-search query
        query_text = f["patch"] or f["filename"]
        related_chunks = retrieve_related_context(vector_store, query_text, k=4)
        context_map[f["filename"]] = related_chunks

    return {"retrieved_context": context_map}


# ---------- Node 3: static_analysis ----------

def static_analysis_node(state: ReviewState) -> Dict[str, Any]:
    findings = []

    for f in state["pr_files"]:
        filename = f["filename"]
        if filename.endswith(".py"):
            findings.extend(_run_ruff(filename))
        # Extend here later for eslint (.js/.ts) etc.

    return {"lint_findings": findings}


def _run_ruff(filename: str) -> list:
    """Run ruff on a single file if it exists locally; return structured findings."""
    if not os.path.exists(filename):
        return []
    try:
        result = subprocess.run(
            ["ruff", "check", "--output-format=json", filename],
            capture_output=True, text=True, timeout=30
        )
        if not result.stdout.strip():
            return []
        raw = json.loads(result.stdout)
        return [
            {
                "file": item["filename"],
                "line": item["location"]["row"],
                "message": item["message"],
                "code": item["code"],
            }
            for item in raw
        ]
    except Exception:
        return []


# ---------- Node 4: llm_review ----------

_client = None

def get_llm_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()  # reads ANTHROPIC_API_KEY from env automatically
    return _client


def llm_review_node(state: ReviewState) -> Dict[str, Any]:
    client = get_llm_client()
    all_findings = []

    for f in state["pr_files"]:
        filename = f["filename"]
        patch = f["patch"]
        if not patch:
            continue  # e.g. binary files, renames with no content change

        related_context = "\n\n".join(state["retrieved_context"].get(filename, []))
        lint_for_file = [lf for lf in state["lint_findings"] if lf.get("file") == filename]

        prompt = f"""You are reviewing a code change in file: {filename}

Diff:
{patch}

Related context from elsewhere in the repo (functions/classes this code touches):
{related_context or "None found."}

Static analysis findings for this file:
{json.dumps(lint_for_file) if lint_for_file else "None."}

Review this change for bugs, security issues, code quality, and maintainability concerns.
Return ONLY a JSON array (no prose, no markdown fences) of findings, each with:
- "line": int (line number in the diff/file)
- "severity": "critical" | "major" | "minor" | "nit"
- "category": "bug" | "security" | "style" | "maintainability"
- "comment": string (concise, actionable, under 40 words)

If there are no issues, return an empty array []."""

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = []

        for item in parsed:
            item["file"] = filename
            all_findings.append(item)

    return {"review_comments": all_findings}


# ---------- Node 5: prioritize ----------

_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "nit": 3}

def prioritize_node(state: ReviewState) -> Dict[str, Any]:
    findings = state["review_comments"]
    sorted_findings = sorted(findings, key=lambda f: _SEVERITY_ORDER.get(f.get("severity", "nit"), 3))
    has_critical = any(f.get("severity") == "critical" for f in findings)

    return {
        "prioritized_comments": sorted_findings,
        "has_critical": has_critical,
    }


# ---------- Node 6: post_comments ----------

def post_comments_node(state: ReviewState) -> Dict[str, Any]:
    findings = state["prioritized_comments"]

    # Only post inline comments for major/critical to avoid noisy PRs
    inline = [
        {"path": f["file"], "line": f["line"], "body": f"**[{f['severity'].upper()}] {f['category']}**: {f['comment']}"}
        for f in findings
        if f.get("severity") in ("critical", "major")
    ]

    summary_lines = [f"### Automated Code Review\n", f"Found **{len(findings)}** issue(s):\n"]
    severity_counts = {"critical": 0, "major": 0, "minor": 0, "nit": 0}
    for sev in ("critical", "major", "minor", "nit"):
        count = sum(1 for f in findings if f.get("severity") == sev)
        severity_counts[sev] = count
        if count:
            summary_lines.append(f"- {sev.capitalize()}: {count}")

    summary = "\n".join(summary_lines)

    post_review(
        repo_full_name=state["repo_full_name"],
        pr_number=state["pr_number"],
        summary_body=summary,
        inline_comments=inline,
        request_changes=state["has_critical"],
    )

    # Report this completed review to TRAC — only after GitHub post succeeds
    trac_summary = (
        f"Reviewed PR #{state['pr_number']} — "
        f"found {severity_counts['critical']} critical, {severity_counts['major']} major issue(s)"
    )
    report_to_trac(
        event_type="agent.review_completed",
        source="code-review-agent",
        summary=trac_summary,
        repo=state["repo_full_name"],
        ref=f"PR #{state['pr_number']}",
        actor="Dushyanth Gadapala",
    )

    return {"final_summary": summary, "posted": True}