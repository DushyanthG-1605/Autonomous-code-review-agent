import re

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
]
_GENERIC = re.compile(
    r"""(?ix)
    \b[\w-]*(api[_-]?key|secret|token|passw(or)?d|pwd)[\w-]*\s*[:=]\s*
    (?P<q>["'])(?P<val>[^"'\s]{8,})(?P=q)
    """
)
_PLACEHOLDER = re.compile(r"(?i)example|your[_-]|placeholder|changeme|xxxx|<.*>|\$\{")


def redact_text(value):
    """Redact secret patterns also recognized by tools/secret_scan.py."""
    for pattern in _PATTERNS:
        value = pattern.sub("[REDACTED]", value)

    def redact_generic(match):
        if _PLACEHOLDER.search(match.group("val")):
            return match.group(0)
        return match.group(0).replace(match.group("val"), "[REDACTED]")

    return _GENERIC.sub(redact_generic, value)


def redact_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value
