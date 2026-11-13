import argparse
import contextlib
import datetime as dt
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import requests

from trac_collector.redaction import redact_text, redact_value

STATE_DIR = Path.home() / ".trac"
EVENT_TYPES = {
    "session.start",
    "session.end",
    "task.started",
    "task.completed",
    "task.blocked",
    "file.edited",
    "command.run",
    "agent.prompt_submitted",
    "git.commit",
    "git.push",
    "git.branch",
}
DATA_FIELDS = {
    "session.start": {"tool_version", "duration_s"},
    "session.end": {"tool_version", "duration_s"},
    "task.started": {"summary", "outcome", "reason"},
    "task.completed": {"summary", "outcome", "reason"},
    "task.blocked": {"summary", "outcome", "reason"},
    "file.edited": {"path", "language", "lines_added", "lines_removed"},
    "command.run": {"kind", "exit_code", "duration_s"},
    "agent.prompt_submitted": {"count"},
    "git.commit": {"sha", "files_changed", "lines_added", "lines_removed"},
    "git.push": {"commits", "remote_branch"},
    "git.branch": {"action", "name"},
}
SESSION_IDLE_SECONDS = 30 * 60
FLUSH_INTERVAL_SECONDS = 30
FLUSH_BATCH_SIZE = 100
MAX_REQUEST_BYTES = 256 * 1024
INLINE_FLUSH_TIMEOUT = 2
MAX_BACKOFF_SECONDS = 300
HOOK_NAMES = ("post-commit", "pre-push", "post-checkout")
CLAUDE_HOOK_COMMAND = "trac hook claude"
CLAUDE_HOOKS = {
    "SessionStart": None,
    "SessionEnd": None,
    "UserPromptSubmit": None,
    "PostToolUse": "Edit|Write|Bash|PowerShell",
    "PostToolUseFailure": "Bash|PowerShell",
}
HOOK_BLOCK_RE = re.compile(
    rb"(?m)^# >>> trac >>>\r?\n.*?^# <<< trac <<<\r?(?:\n|$)",
    re.DOTALL,
)
HOOK_SEPARATOR_MARKER = b"# trac: separator-added"
HOOK_CREATED_MARKER = b"# trac: created-file"
HOOK_MODE_RE = re.compile(rb"(?m)^# trac: original-mode=([0-7]+)\r?$")


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _ulid():
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    chars = ["0"] * 26
    for index in range(25, -1, -1):
        chars[index] = alphabet[value & 31]
        value >>= 5
    return "".join(chars)


def _ensure_state_dir():
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        STATE_DIR.chmod(0o700)
    except OSError:
        pass


def _read_json(path, default):
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json(path, value):
    _ensure_state_dir()
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"), ensure_ascii=True)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temp_path.chmod(0o600)
    except OSError:
        pass
    os.replace(temp_path, path)


@contextlib.contextmanager
def _queue_lock():
    _ensure_state_dir()
    lock_path = STATE_DIR / "queue.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_jsonl(path):
    events = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
    except OSError:
        pass
    return events


def _write_jsonl(path, events):
    _ensure_state_dir()
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, separators=(",", ":"), ensure_ascii=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temp_path.chmod(0o600)
    except OSError:
        pass
    os.replace(temp_path, path)


def _queue_size():
    with _queue_lock():
        return len(_read_jsonl(STATE_DIR / "queue.jsonl"))


def _append_jsonl(path, records):
    if not records:
        return
    _ensure_state_dir()
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _git_output(*args, cwd=None):
    try:
        result = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
            cwd=cwd,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _repo_name(remote_url):
    if not remote_url:
        return None
    if ":" in remote_url and "://" not in remote_url:
        path = remote_url.rsplit(":", 1)[-1]
    else:
        path = urlparse(remote_url).path
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    owner, name = parts[-2:]
    if name.endswith(".git"):
        name = name[:-4]
    return f"{owner}/{name}" if owner and name else None


def _repo_path(value, repo_root):
    if not isinstance(value, str) or not value:
        return value
    path = Path(value)
    if path.is_absolute():
        if not repo_root:
            return None
        try:
            path = path.resolve().relative_to(Path(repo_root).resolve())
        except (OSError, ValueError):
            return None
    else:
        normalized = os.path.normpath(value)
        if normalized == os.pardir or normalized.startswith(os.pardir + os.sep):
            return None
        path = Path(normalized)
    return path.as_posix()


def _read_config():
    value = _read_json(STATE_DIR / "config.json", {})
    return value if isinstance(value, dict) else {}


def _load_session():
    state = _read_json(STATE_DIR / "session.json", {})
    if not isinstance(state, dict):
        state = {}
    env_session_id = os.environ.get("TRAC_SESSION_ID")
    if env_session_id:
        if state.get("session_id") != env_session_id:
            return env_session_id, {}
        return env_session_id, state
    return state.get("session_id") or _ulid(), state


def _build_event(event_type, data, session_id, source, tool, repo, branch):
    return {
        "event_id": _ulid(),
        "schema_version": 1,
        "type": event_type,
        "ts": _utc_now(),
        "session_id": session_id,
        "repo": repo,
        "branch": branch,
        "source": source,
        "tool": tool,
        "data": data,
    }


def _event_data(event_type, summary, outcome, reason, data, repo_root):
    fields = DATA_FIELDS[event_type]
    candidate = {}
    for pair in data:
        key, separator, value = pair.partition("=")
        if not separator:
            continue
        candidate[key] = value
    if summary is not None:
        candidate["summary"] = summary[:200]
    if outcome is not None:
        candidate["outcome"] = outcome
    if reason is not None:
        candidate["reason"] = reason

    filtered = {key: value for key, value in candidate.items() if key in fields}
    integer_fields = {
        "lines_added",
        "lines_removed",
        "files_changed",
        "exit_code",
        "count",
        "commits",
    }
    for key in list(filtered):
        try:
            if key in integer_fields:
                filtered[key] = int(filtered[key])
            elif key == "duration_s":
                filtered[key] = float(filtered[key])
                if not math.isfinite(filtered[key]):
                    filtered.pop(key)
        except (TypeError, ValueError):
            filtered.pop(key)
    if "path" in filtered:
        relative_path = _repo_path(filtered["path"], repo_root)
        if relative_path is None:
            filtered.pop("path")
        else:
            filtered["path"] = relative_path
    filtered = redact_value(filtered)
    if "summary" in filtered and isinstance(filtered["summary"], str):
        filtered["summary"] = filtered["summary"][:200]
    return filtered


def _session_events(
    event_type,
    data,
    session_id,
    session_state,
    source,
    tool,
    repo,
    branch,
    now,
    preserve_session_id=False,
):
    emitted = []
    last_activity = session_state.get("last_activity")
    elapsed = now - last_activity if isinstance(last_activity, (int, float)) else None
    if event_type not in {"session.start", "session.end"}:
        if elapsed is None:
            emitted.append(_build_event("session.start", {}, session_id, source, tool, repo, branch))
            session_state["started_at"] = now
        elif elapsed > SESSION_IDLE_SECONDS and not preserve_session_id:
            started_at = session_state.get("started_at")
            duration_s = max(0, int(now - started_at)) if isinstance(started_at, (int, float)) else int(elapsed)
            emitted.append(
                _build_event(
                    "session.end",
                    {"duration_s": duration_s},
                    session_id,
                    source,
                    tool,
                    repo,
                    branch,
                )
            )
            session_id = os.environ.get("TRAC_SESSION_ID") or _ulid()
            emitted.append(_build_event("session.start", {}, session_id, source, tool, repo, branch))
            session_state.clear()
            session_state["started_at"] = now

    event_data = data
    if event_type == "session.end" and "duration_s" not in event_data:
        started_at = session_state.get("started_at")
        if isinstance(started_at, (int, float)):
            event_data = dict(event_data)
            event_data["duration_s"] = max(0, int(now - started_at))
    event = _build_event(event_type, event_data, session_id, source, tool, repo, branch)
    emitted.append(event)
    if event_type == "session.end":
        session_state.clear()
    else:
        session_state["session_id"] = session_id
        session_state["last_activity"] = now
        if event_type == "session.start" or not isinstance(session_state.get("started_at"), (int, float)):
            session_state["started_at"] = now
    return emitted, session_id


def emit_event(
    event_type,
    summary=None,
    outcome=None,
    reason=None,
    data=None,
    source="skill",
    tool=None,
    session_id=None,
    cwd=None,
):
    requested_session_id = session_id
    if (STATE_DIR / "paused").exists() or event_type not in EVENT_TYPES:
        return

    _ensure_state_dir()
    data = data or []
    git_options = {"cwd": cwd} if cwd is not None else {}
    repo_root = _git_output("rev-parse", "--show-toplevel", **git_options)
    repo = _repo_name(_git_output("remote", "get-url", "origin", **git_options))
    branch = _git_output("rev-parse", "--abbrev-ref", "HEAD", **git_options)
    filtered_data = _event_data(event_type, summary, outcome, reason, data, repo_root)
    if summary is not None:
        summary = redact_text(summary)[:200]
        if "summary" in filtered_data:
            filtered_data["summary"] = summary
    now = time.time()
    with _queue_lock():
        if (STATE_DIR / "paused").exists():
            return
        loaded_session_id, session_state = _load_session()
        preserve_session_id = isinstance(requested_session_id, str) and bool(
            requested_session_id
        )
        if preserve_session_id:
            session_id = requested_session_id
            if session_state.get("session_id") != session_id:
                session_state = {}
        else:
            session_id = loaded_session_id
        events, _ = _session_events(
            event_type,
            filtered_data,
            session_id,
            session_state,
            source,
            tool,
            repo,
            branch,
            now,
            preserve_session_id=preserve_session_id,
        )
        events = [redact_value(event) for event in events]
        path = STATE_DIR / "queue.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, separators=(",", ":"), ensure_ascii=True))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _write_json(STATE_DIR / "session.json", session_state)

    state = _read_json(STATE_DIR / "state.json", {})
    last_flush = state.get("last_flush_attempt") if isinstance(state, dict) else None
    queue_size = _queue_size()
    if queue_size >= 50 or not isinstance(last_flush, (int, float)) or now - last_flush > FLUSH_INTERVAL_SECONDS:
        try:
            flush_queue(timeout=INLINE_FLUSH_TIMEOUT)
        except BaseException:
            return


def _read_state():
    state = _read_json(STATE_DIR / "state.json", {})
    return state if isinstance(state, dict) else {}


def _write_state(state):
    with _queue_lock():
        _write_json(STATE_DIR / "state.json", state)


def _record_rejected(events, reason, details=None):
    now = _utc_now()
    records = [
        {
            "rejected_at": now,
            "reason": reason,
            "details": details,
            "event": event,
        }
        for event in events
    ]
    with _queue_lock():
        _append_jsonl(STATE_DIR / "rejected.log", records)


def _record_sent(events):
    cutoff = time.time() - 7 * 24 * 60 * 60
    path = STATE_DIR / "sent.jsonl"
    with _queue_lock():
        old_records = _read_jsonl(path)
        kept = [
            record
            for record in old_records
            if isinstance(record.get("sent_at_epoch"), (int, float))
            and record["sent_at_epoch"] >= cutoff
        ]
        sent_at = _utc_now()
        sent_at_epoch = time.time()
        kept.extend(
            {"sent_at": sent_at, "sent_at_epoch": sent_at_epoch, "event": event}
            for event in events
        )
        _write_jsonl(path, kept)


def _remove_from_queue(event_ids):
    event_ids = set(event_ids)
    with _queue_lock():
        path = STATE_DIR / "queue.jsonl"
        remaining = [
            event for event in _read_jsonl(path)
            if event.get("event_id") not in event_ids
        ]
        _write_jsonl(path, remaining)


def _mark_attempt(state, now=None):
    state["last_flush_attempt"] = time.time() if now is None else now
    _write_state(state)


def _schedule_backoff(state, retry_after=None):
    backoff = min(int(state.get("backoff_seconds", 0)) * 2 or 1, MAX_BACKOFF_SECONDS)
    if retry_after is not None:
        backoff = max(backoff, retry_after)
    state["backoff_seconds"] = backoff
    state["backoff_until"] = time.time() + backoff
    _mark_attempt(state)


def _drop_batch(batch, rejected, rejection_reason=None, details=None):
    rejected_ids = {
        item.get("event_id")
        for item in rejected
        if isinstance(item, dict) and item.get("event_id")
    }
    rejected_events = [event for event in batch if event.get("event_id") in rejected_ids]
    accepted_events = [event for event in batch if event.get("event_id") not in rejected_ids]
    if rejection_reason:
        rejected_events = batch
        accepted_events = []
    _record_rejected(
        rejected_events,
        rejection_reason or "event_rejected",
        details if rejection_reason else rejected,
    )
    _record_sent(accepted_events)
    _remove_from_queue(event.get("event_id") for event in batch)


def _batches(events):
    batches = []
    current = []
    for event in events:
        candidate = current + [event]
        encoded = json.dumps(
            {"events": candidate}, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        if current and (
            len(candidate) > FLUSH_BATCH_SIZE or len(encoded) > MAX_REQUEST_BYTES
        ):
            batches.append(current)
            current = [event]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def flush_queue(timeout=5, force=False):
    if (STATE_DIR / "paused").exists():
        return {"sent": 0, "queued": _queue_size(), "paused": True}
    config = _read_config()
    url = config.get("url")
    token = config.get("token")
    if not isinstance(url, str) or not isinstance(token, str) or not token:
        return {"sent": 0, "queued": _queue_size(), "logged_in": False}

    state = _read_state()
    if not force and state.get("backoff_until", 0) > time.time():
        return {"sent": 0, "queued": _queue_size(), "backoff": True}

    with _queue_lock():
        queued = _read_jsonl(STATE_DIR / "queue.jsonl")
    oversize = [
        event
        for event in queued
        if len(
            json.dumps(
                {"events": [event]}, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        ) > MAX_REQUEST_BYTES
    ]
    if oversize:
        _drop_batch(oversize, [], "event_too_large")
        oversized_ids = {event.get("event_id") for event in oversize}
        queued = [event for event in queued if event.get("event_id") not in oversized_ids]
    sent_count = 0
    endpoint = url.rstrip("/") + "/v1/events"
    pending_batches = deque(_batches(queued))
    while pending_batches:
        batch = pending_batches.popleft()
        state["last_flush_attempt"] = time.time()
        _write_state(state)
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                json={"events": batch},
                timeout=timeout,
            )
        except requests.RequestException:
            _schedule_backoff(state)
            return {"sent": sent_count, "queued": _queue_size(), "network_error": True}

        if response.status_code == 401:
            state["auth_error"] = True
            _mark_attempt(state)
            return {"sent": sent_count, "queued": _queue_size(), "auth_error": True}
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = None
            try:
                retry_after = int(response.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                pass
            _schedule_backoff(state, retry_after)
            return {"sent": sent_count, "queued": _queue_size(), "retry": True}
        if response.status_code == 422:
            try:
                details = response.json()
            except ValueError:
                details = response.text[:300]
            _drop_batch(batch, [], "http_422", details)
            _mark_attempt(state)
            continue
        if response.status_code == 413:
            if len(batch) == 1:
                _drop_batch(batch, [], "event_too_large", response.text[:300])
                _mark_attempt(state)
                continue
            midpoint = len(batch) // 2
            pending_batches.appendleft(batch[midpoint:])
            pending_batches.appendleft(batch[:midpoint])
            continue
        if response.status_code != 202:
            _drop_batch(
                batch,
                [],
                f"http_{response.status_code}",
                response.text[:300],
            )
            _mark_attempt(state)
            continue

        try:
            result = response.json()
            accepted = int(result["accepted"])
            duplicates = int(result["duplicates"])
            rejected = result["rejected"]
            if not isinstance(rejected, list) or accepted + duplicates + len(rejected) != len(batch):
                raise ValueError("Invalid ingestion response counts")
        except (KeyError, TypeError, ValueError):
            _schedule_backoff(state)
            return {"sent": sent_count, "queued": _queue_size(), "invalid_response": True}

        _drop_batch(batch, rejected)
        sent_count += accepted + duplicates
        state["last_success_at"] = _utc_now()
        state.pop("auth_error", None)
        state["backoff_seconds"] = 0
        state["backoff_until"] = 0
        _mark_attempt(state)

    return {"sent": sent_count, "queued": _queue_size()}


def _hook_block(name):
    scripts = {
        "post-commit": """if command -v trac >/dev/null 2>&1; then
    sha=$(git rev-parse HEAD 2>/dev/null || :)
    stats=$(git show --shortstat --format= HEAD 2>/dev/null || :)
    set -- $(printf '%s\\n' "$stats" | awk '{
        for (i = 1; i <= NF; i++) {
            if ($i == "file" || $i == "files") files = $(i - 1)
            if ($i ~ /^insertion/) added = $(i - 1)
            if ($i ~ /^deletion/) removed = $(i - 1)
        }
        gsub(/,/, "", files)
        gsub(/,/, "", added)
        gsub(/,/, "", removed)
    } END {
        print (files == "" ? 0 : files), (added == "" ? 0 : added), (removed == "" ? 0 : removed)
    }')
    files=${1:-0}
    added=${2:-0}
    removed=${3:-0}
    if [ -n "$sha" ]; then
        trac emit git.commit --source git-hook --tool git --data \\
            "sha=$sha" "files_changed=$files" "lines_added=$added" "lines_removed=$removed" || true
    fi
fi""",
        "pre-push": """if command -v trac >/dev/null 2>&1; then
    while IFS=' ' read -r local_ref local_sha remote_ref remote_sha || [ -n "$local_ref" ]; do
        case "$remote_ref" in
            refs/heads/*) remote_branch=${remote_ref#refs/heads/} ;;
            *) continue ;;
        esac
        case "$local_sha" in
            *[!0]*)
                case "$remote_sha" in
                    *[!0]*) commits=$(git rev-list --count "$remote_sha..$local_sha" 2>/dev/null || printf '0') ;;
                    *) commits=$(git rev-list --count "$local_sha" 2>/dev/null || printf '0') ;;
                esac
                ;;
            *) commits=0 ;;
        esac
        case "$commits" in
            ''|*[!0-9]*) commits=0 ;;
        esac
        trac emit git.push --source git-hook --tool git --data \\
            "commits=$commits" "remote_branch=$remote_branch" || true
    done
fi""",
        "post-checkout": """if [ "$3" = "1" ] && command -v trac >/dev/null 2>&1; then
    branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || :)
    if [ -n "$branch" ]; then
        action=switched
        branch_subject=$(git reflog -1 --format=%gs "refs/heads/$branch" 2>/dev/null || :)
        branch_time=$(git reflog -1 --date=unix --format=%gD "refs/heads/$branch" 2>/dev/null | sed 's/.*@{//;s/}//')
        head_time=$(git reflog -1 --date=unix --format=%gD HEAD 2>/dev/null | sed 's/.*@{//;s/}//')
        case "$branch_subject" in
            "branch: Created from "*)
                if [ -n "$branch_time" ] && [ "$branch_time" = "$head_time" ]; then
                    action=created
                fi
                ;;
        esac
        trac emit git.branch --source git-hook --tool git --data \\
            "action=$action" "name=$branch" || true
    fi
fi""",
    }
    if name not in scripts:
        raise ValueError(f"Unsupported Git hook: {name}")
    return f"# >>> trac >>>\n{scripts[name]}\n# <<< trac <<<".encode("utf-8")


def _managed_hook_content(existing, block, previous_mode=None):
    matches = list(HOOK_BLOCK_RE.finditer(existing))
    if matches:
        mode_match = HOOK_MODE_RE.search(matches[0].group(0))
        if mode_match:
            block = block.replace(
                b"# >>> trac >>>\n",
                b"# >>> trac >>>\n# trac: original-mode="
                + mode_match.group(1)
                + b"\n",
                1,
            )
        elif previous_mode is not None:
            mode = stat.S_IMODE(previous_mode)
            block = block.replace(
                b"# >>> trac >>>\n",
                b"# >>> trac >>>\n"
                + f"# trac: original-mode={mode:o}\n".encode("ascii"),
                1,
            )
        if HOOK_SEPARATOR_MARKER in matches[0].group(0):
            block = block.replace(
                b"# >>> trac >>>\n",
                b"# >>> trac >>>\n" + HOOK_SEPARATOR_MARKER + b"\n",
                1,
            )
        if HOOK_CREATED_MARKER in matches[0].group(0):
            block = block.replace(
                b"# >>> trac >>>\n",
                b"# >>> trac >>>\n" + HOOK_CREATED_MARKER + b"\n",
                1,
            )
        result = existing
        for index, match in reversed(list(enumerate(matches))):
            newline = b"\n" if match.group(0).endswith((b"\n", b"\r")) else b""
            replacement = block + newline if index == 0 else b""
            result = result[:match.start()] + replacement + result[match.end():]
        return result
    if not existing:
        block = block.replace(
            b"# >>> trac >>>\n",
            b"# >>> trac >>>\n" + HOOK_CREATED_MARKER + b"\n",
            1,
        )
        return b"#!/bin/sh\n" + block + b"\n"
    if previous_mode is not None:
        mode = stat.S_IMODE(previous_mode)
        block = block.replace(
            b"# >>> trac >>>\n",
            b"# >>> trac >>>\n"
            + f"# trac: original-mode={mode:o}\n".encode("ascii"),
            1,
        )
    if existing.endswith((b"\n", b"\r")):
        return existing + block + b"\n"
    block = block.replace(
        b"# >>> trac >>>\n",
        b"# >>> trac >>>\n" + HOOK_SEPARATOR_MARKER + b"\n",
        1,
    )
    return existing + b"\n" + block + b"\n"


def _write_hook(path, content, previous_mode=None, ensure_executable=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".trac-" + secrets.token_hex(4))
    temp_path.write_bytes(content)
    mode = previous_mode
    if mode is None:
        mode = temp_path.stat().st_mode
    try:
        os.chmod(temp_path, mode | 0o111 if ensure_executable else mode)
    except OSError:
        pass
    os.replace(temp_path, path)


def _current_hooks_dir():
    repo_root_text = _git_output("rev-parse", "--show-toplevel")
    if not repo_root_text:
        raise RuntimeError("Not inside a Git working tree")
    repo_root = Path(repo_root_text).resolve()
    hooks_path_text = _git_output("rev-parse", "--git-path", "hooks", cwd=repo_root)
    if not hooks_path_text:
        raise RuntimeError("Could not determine this repository's hooks directory")
    hooks_path = Path(hooks_path_text).expanduser()
    if not hooks_path.is_absolute():
        hooks_path = repo_root / hooks_path
    return hooks_path.resolve()


def init_hooks():
    hooks_dir = _current_hooks_dir()
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        path = hooks_dir / name
        exists = path.exists()
        existing = path.read_bytes() if exists else b""
        previous_mode = path.stat().st_mode if exists else None
        content = _managed_hook_content(
            existing,
            _hook_block(name),
            previous_mode,
        )
        if not exists or content != existing:
            _write_hook(path, content, previous_mode)
        elif exists:
            os.chmod(path, previous_mode | 0o111)
    return hooks_dir


def uninit_hooks():
    hooks_dir = _current_hooks_dir()
    for name in HOOK_NAMES:
        path = hooks_dir / name
        if not path.is_file():
            continue
        content = path.read_bytes()
        matches = list(HOOK_BLOCK_RE.finditer(content))
        if not matches:
            continue
        created_file = any(
            HOOK_CREATED_MARKER in match.group(0) for match in matches
        )
        mode_match = HOOK_MODE_RE.search(matches[0].group(0))
        updated = content
        for match in reversed(matches):
            start = match.start()
            if (
                HOOK_SEPARATOR_MARKER in match.group(0)
                and start > 0
                and content[start - 1:start] == b"\n"
                and match.end() == len(content)
            ):
                start -= 1
            updated = updated[:start] + updated[match.end():]
        if not updated.strip() or (
            created_file and updated.strip() == b"#!/bin/sh"
        ):
            path.unlink()
        else:
            previous_mode = path.stat().st_mode
            if mode_match:
                previous_mode = int(mode_match.group(1), 8)
            _write_hook(
                path,
                updated,
                previous_mode,
                ensure_executable=False,
            )
    return hooks_dir


def _claude_settings_path():
    repo_root = _git_output("rev-parse", "--show-toplevel")
    if not repo_root:
        raise RuntimeError("Not inside a Git working tree")
    return Path(repo_root) / ".claude" / "settings.json"


def init_claude_hooks():
    settings_path = _claude_settings_path()
    if settings_path.exists():
        try:
            with settings_path.open(encoding="utf-8") as handle:
                settings = json.load(handle)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Could not read {settings_path}: {exc}") from exc
        if not isinstance(settings, dict):
            raise RuntimeError(f"{settings_path} must contain a JSON object")
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError(f"The hooks setting in {settings_path} must be an object")

    changed = False
    for event_name, matcher in CLAUDE_HOOKS.items():
        groups = hooks.setdefault(event_name, [])
        if not isinstance(groups, list):
            raise TypeError(f"The {event_name} hooks setting must be an array")
        installed = any(
            (group.get("matcher") if isinstance(group, dict) else None) == matcher
            and isinstance(group, dict)
            and any(
                isinstance(handler, dict)
                and handler.get("type") == "command"
                and handler.get("command") == CLAUDE_HOOK_COMMAND
                for handler in group.get("hooks", [])
            )
            for group in groups
        )
        if not installed:
            group = {
                "hooks": [
                    {"type": "command", "command": CLAUDE_HOOK_COMMAND}
                ]
            }
            if matcher is not None:
                group["matcher"] = matcher
            groups.append(group)
            changed = True

    if changed:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = settings_path.with_name(settings_path.name + ".trac-tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if settings_path.exists():
            try:
                os.chmod(temp_path, stat.S_IMODE(settings_path.stat().st_mode))
            except OSError:
                pass
        os.replace(temp_path, settings_path)
    return settings_path


def _line_count(value):
    return len(value.splitlines()) if isinstance(value, str) else 0


def _language_for_path(file_path):
    suffix = Path(file_path).suffix.lower()
    languages = {
        ".c": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cs": "csharp",
        ".css": "css",
        ".go": "go",
        ".h": "c",
        ".hpp": "cpp",
        ".html": "html",
        ".java": "java",
        ".js": "javascript",
        ".jsx": "javascript",
        ".json": "json",
        ".md": "markdown",
        ".php": "php",
        ".py": "python",
        ".rb": "ruby",
        ".rs": "rust",
        ".sh": "shell",
        ".sql": "sql",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".yaml": "yaml",
        ".yml": "yaml",
    }
    return languages.get(suffix, "other")


def _command_kind(command):
    patterns = {
        "test": r"\b(pytest|unittest|tox|jest|vitest|mocha|test|testing)\b",
        "build": r"\b(build|compile|make|webpack|vite\s+build)\b",
        "lint": r"\b(lint|ruff|flake8|mypy|pyright|eslint|prettier)\b",
    }
    for kind, pattern in patterns.items():
        if re.search(pattern, command, re.IGNORECASE):
            return kind
    return "other"


def _claude_hook():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return

        hook_event = payload.get("hook_event_name")
        session_id = payload.get("session_id")
        session_id = session_id if isinstance(session_id, str) else None
        cwd = payload.get("cwd")
        cwd = cwd if isinstance(cwd, str) else None
        common = {
            "source": "claude-code",
            "tool": "claude-code",
            "session_id": session_id,
            "cwd": cwd,
        }

        if hook_event == "SessionStart":
            emit_event("session.start", **common)
        elif hook_event == "SessionEnd":
            emit_event("session.end", **common)
        elif hook_event == "UserPromptSubmit":
            emit_event("agent.prompt_submitted", data=["count=1"], **common)
        elif hook_event in {"PostToolUse", "PostToolUseFailure"}:
            tool_name = payload.get("tool_name")
            tool_input = payload.get("tool_input")
            if not isinstance(tool_input, dict):
                return

            if hook_event == "PostToolUse" and tool_name in {"Edit", "Write"}:
                file_path = tool_input.get("file_path")
                if not isinstance(file_path, str) or not file_path:
                    return
                if tool_name == "Edit":
                    old_text = tool_input.get("old_string", "")
                    new_text = tool_input.get("new_string", "")
                else:
                    old_text = ""
                    new_text = tool_input.get("content", "")
                emit_event(
                    "file.edited",
                    data=[
                        f"path={file_path}",
                        f"language={_language_for_path(file_path)}",
                        f"lines_added={_line_count(new_text)}",
                        f"lines_removed={_line_count(old_text)}",
                    ],
                    **common,
                )
            elif tool_name in {"Bash", "PowerShell"}:
                command = tool_input.get("command")
                if not isinstance(command, str):
                    return
                data = [f"kind={_command_kind(command)}"]
                if hook_event == "PostToolUse":
                    response = payload.get("tool_response")
                    exit_code = response.get("exit_code", 0) if isinstance(response, dict) else 0
                    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                        data.append(f"exit_code={exit_code}")
                else:
                    error = payload.get("error")
                    match = re.match(r"\s*Exit code (-?\d+)\b", error) if isinstance(error, str) else None
                    if match:
                        data.append(f"exit_code={match.group(1)}")
                emit_event("command.run", data=data, **common)
    except Exception:  # noqa: BLE001 - hooks must never block their caller.
        return


def _prune_sent_records():
    cutoff = time.time() - 7 * 24 * 60 * 60
    path = STATE_DIR / "sent.jsonl"
    with _queue_lock():
        records = _read_jsonl(path)
        kept = [
            record
            for record in records
            if isinstance(record.get("sent_at_epoch"), (int, float))
            and record["sent_at_epoch"] >= cutoff
        ]
        if len(kept) != len(records):
            _write_jsonl(path, kept)
        return kept


def _status():
    state = _read_state()
    queued = _queue_size()
    paused = (STATE_DIR / "paused").exists()
    config = _read_config()
    print(f"Tracking: {'paused' if paused else 'on'}")
    print(f"Login: {'logged in' if config.get('token') else 'not logged in'}")
    print(f"Authentication: {'required' if state.get('auth_error') else 'ok'}")
    print(f"Queued: {queued}")
    print(f"Last successful send: {state.get('last_success_at', 'never')}")


def _print_data():
    queued = _read_jsonl(STATE_DIR / "queue.jsonl")
    sent = _prune_sent_records()
    print(json.dumps({"queued": queued, "sent": sent}, indent=2, ensure_ascii=True))


def _save_login(url, token):
    _ensure_state_dir()
    config = _read_config()
    config.update({"url": url.rstrip("/"), "token": token})
    _write_json(STATE_DIR / "config.json", config)
    state = _read_state()
    state.pop("auth_error", None)
    _write_state(state)


def _logout():
    config = _read_config()
    config.pop("token", None)
    if config:
        _write_json(STATE_DIR / "config.json", config)
    else:
        try:
            (STATE_DIR / "config.json").unlink()
        except FileNotFoundError:
            pass
    state = _read_state()
    state.pop("auth_error", None)
    _write_state(state)


def _parser():
    parser = argparse.ArgumentParser(prog="trac")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login")
    login.add_argument("--url", required=True)
    login.add_argument("--token", required=True)

    emit = subparsers.add_parser("emit")
    emit.add_argument("type")
    emit.add_argument("--summary")
    emit.add_argument("--outcome")
    emit.add_argument("--reason")
    emit.add_argument("--data", nargs="*", default=[])
    emit.add_argument("--source", default="skill")
    emit.add_argument("--tool")

    subparsers.add_parser("flush")
    subparsers.add_parser("status")
    subparsers.add_parser("pause")
    subparsers.add_parser("resume")
    subparsers.add_parser("data")
    subparsers.add_parser("logout")
    init = subparsers.add_parser("init")
    init.add_argument("--claude", action="store_true")
    subparsers.add_parser("uninit")
    hook = subparsers.add_parser("hook")
    hook_subparsers = hook.add_subparsers(dest="hook_name", required=True)
    hook_subparsers.add_parser("claude")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] == ["hook", "claude"]:
        _claude_hook()
        return 0

    parser = _parser()
    is_emit = bool(argv and argv[0] == "emit")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if is_emit:
            return 0
        raise exc

    if args.command == "emit":
        try:
            emit_event(
                args.type,
                summary=args.summary,
                outcome=args.outcome,
                reason=args.reason,
                data=args.data,
                source=args.source,
                tool=args.tool,
            )
        except BaseException:
            pass
        return 0

    try:
        if args.command == "login":
            _save_login(args.url, args.token)
            print("Logged in.")
        elif args.command == "flush":
            result = flush_queue(force=True)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "status":
            _status()
        elif args.command == "pause":
            _ensure_state_dir()
            with _queue_lock():
                (STATE_DIR / "paused").touch()
        elif args.command == "resume":
            with _queue_lock():
                try:
                    (STATE_DIR / "paused").unlink()
                except FileNotFoundError:
                    pass
        elif args.command == "data":
            _print_data()
        elif args.command == "logout":
            _logout()
            print("Logged out.")
        elif args.command == "init":
            print(f"Installed TRAC hooks in {init_hooks()}")
            if args.claude:
                print(f"Installed Claude Code hooks in {init_claude_hooks()}")
        elif args.command == "uninit":
            print(f"Removed TRAC hooks from {uninit_hooks()}")
    except Exception as exc:
        print(f"trac: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
