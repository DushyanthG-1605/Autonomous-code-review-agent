"""Lightweight secret scanner for the added lines of a PR patch.

Complements ruff, whose S105 rule misses names like API_KEY. Findings never
include the secret value itself.
"""
import re
from typing import Any, Dict, List

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("API key (sk- prefix)", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
]

_GENERIC = re.compile(
    r"""(?ix)
    \b[\w-]*(api[_-]?key|secret|token|passw(or)?d|pwd)[\w-]*\s*[:=]\s*
    (?P<q>["'])(?P<val>[^"'\s]{8,})(?P=q)
    """
)
_PLACEHOLDER = re.compile(r"(?i)example|your[_-]|placeholder|changeme|xxxx|<.*>|\$\{")


def scan_patch(filename: str, patch: str) -> List[Dict[str, Any]]:
    """Return findings for secrets on added lines, with new-file line numbers."""
    findings: List[Dict[str, Any]] = []
    new_line = 0

    for raw in patch.splitlines():
        hunk = _HUNK.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if raw.startswith("\\"):          # "\ No newline at end of file"
            continue
        if raw.startswith("-"):           # removed line: no new-file line number
            continue

        if raw.startswith("+"):
            text = raw[1:]
            label = next((name for name, rx in _PATTERNS if rx.search(text)), None)
            if label is None:
                m = _GENERIC.search(text)
                if m and not _PLACEHOLDER.search(m.group("val")):
                    label = "hardcoded credential"
            if label:
                findings.append({
                    "file": filename,
                    "line": new_line,
                    "code": "SECRET",
                    "message": f"Possible hardcoded secret ({label}).",
                })
        new_line += 1

    return findings
