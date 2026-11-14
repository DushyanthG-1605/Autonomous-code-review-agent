import argparse
import contextlib
import datetime as dt
import getpass
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import uuid
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
MAX_QUEUE_EVENTS = 1000
MAX_QUEUE_BYTES = 2 * 1024 * 1024
KEYRING_SERVICE = "trac-activity-collector"
TASK_EVENT_FIELDS = {
    "task.started": set(),
    "task.progress": {"progress", "note"},
    "task.blocked": {"reason_code", "note"},
    "task.completed": {"outcome", "note"},
}
TASK_BLOCK_REASONS = {
    "dependency",
    "permission",
    "test_failure",
    "build_failure",
    "external",
    "other",
}
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


def _append_bounded_queue(events):
    path = STATE_DIR / "queue.jsonl"
    queued = _read_jsonl(path)
    queued.extend(events)
    dropped = 0
    while queued and (
        len(queued) > MAX_QUEUE_EVENTS
        or sum(
            len(json.dumps(item, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
            + 1
            for item in queued
        )
        > MAX_QUEUE_BYTES
    ):
        queued.pop(0)
        dropped += 1
    _write_jsonl(path, queued)
    if dropped:
        state = _read_json(STATE_DIR / "state.json", {})
        if not isinstance(state, dict):
            state = {}
        state["dropped_queue_events"] = state.get("dropped_queue_events", 0) + dropped
        _write_json(STATE_DIR / "state.json", state)


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
    if not isinstance(value, dict):
        value = {}
    legacy_token = value.pop("token", None)
    if isinstance(legacy_token, str) and legacy_token:
        _store_credential(legacy_token)
        _write_json(STATE_DIR / "config.json", value)
    token = _load_credential()
    if token:
        value["token"] = token
    return value


def _credential_account():
    return str(STATE_DIR.expanduser().resolve())


def _load_credential():
    try:
        import keyring
    except ImportError:
        if os.name == "nt":
            raise RuntimeError(
                "Install the TRAC collector with its keyring dependency before login"
            )
        value = _read_json(STATE_DIR / "credential.json", {})
        if isinstance(value, dict):
            token = value.get("token")
            if isinstance(token, str):
                return token
        return None
    try:
        token = keyring.get_password(KEYRING_SERVICE, _credential_account())
        if token:
            return token
    except keyring.errors.KeyringError as exc:
        raise RuntimeError("The operating-system credential store is unavailable") from exc
    value = _read_json(STATE_DIR / "credential.json", {})
    if isinstance(value, dict) and isinstance(value.get("token"), str):
        token = value["token"]
        _store_credential(token)
        (STATE_DIR / "credential.json").unlink()
        return token
    return None


def _store_credential(token):
    try:
        import keyring
    except ImportError:
        if os.name == "nt":
            raise RuntimeError(
                "Install the TRAC collector with its keyring dependency before login"
            )
        _write_json(STATE_DIR / "credential.json", {"token": token})
        return "restricted-file"
    try:
        keyring.set_password(KEYRING_SERVICE, _credential_account(), token)
        fallback_path = STATE_DIR / "credential.json"
        if fallback_path.exists():
            fallback_path.unlink()
        return "keyring"
    except keyring.errors.KeyringError as exc:
        raise RuntimeError("The operating-system credential store is unavailable") from exc


def _delete_credential():
    try:
        import keyring
    except ImportError:
        keyring = None
    if keyring is not None:
        try:
            keyring.delete_password(KEYRING_SERVICE, _credential_account())
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError as exc:
            raise RuntimeError(
                "The operating-system credential store is unavailable"
            ) from exc
    try:
        (STATE_DIR / "credential.json").unlink()
    except FileNotFoundError:
        pass


def _write_config(config):
    safe_config = dict(config)
    safe_config.pop("token", None)
    _write_json(STATE_DIR / "config.json", safe_config)


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
        _append_bounded_queue(events)
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
    scopes = config.get("scopes")
    if not isinstance(scopes, list) or "events:ingest" not in scopes:
        return {"sent": 0, "queued": _queue_size(), "scope_error": True}

    state = _read_state()
    if not force and state.get("backoff_until", 0) > time.time():
        return {"sent": 0, "queued": _queue_size(), "backoff": True}

    with _queue_lock():
        queued = _read_jsonl(STATE_DIR / "queue.jsonl")
    rejected_count = 0
    if config.get("project_id"):
        invalid = [
            event
            for event in queued
            if event.get("schema_version") == 2
            and not _valid_queued_canonical_event(event, config["project_id"])
        ]
        if invalid:
            _drop_batch(invalid, [], "invalid_local_event")
            rejected_count += len(invalid)
            invalid_ids = {event.get("event_id") for event in invalid}
            queued = [event for event in queued if event.get("event_id") not in invalid_ids]
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
        if response.status_code == 403:
            state["scope_error"] = True
            _mark_attempt(state)
            return {"sent": sent_count, "queued": _queue_size(), "scope_error": True}
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
            rejected_count += len(batch)
            _mark_attempt(state)
            continue
        if response.status_code == 413:
            if len(batch) == 1:
                _drop_batch(batch, [], "event_too_large", response.text[:300])
                rejected_count += 1
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
            rejected_count += len(batch)
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
        rejected_count += len(rejected)
        sent_count += accepted + duplicates
        state["last_success_at"] = _utc_now()
        state.pop("auth_error", None)
        state.pop("scope_error", None)
        state["backoff_seconds"] = 0
        state["backoff_until"] = 0
        _mark_attempt(state)

    return {
        "sent": sent_count,
        "queued": _queue_size(),
        "rejected": rejected_count,
    }


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


def _validate_server_url(value):
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use a valid TRAC server URL without embedded credentials")
    if parsed.scheme != "https" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("HTTPS is required except for loopback development servers")
    return value.rstrip("/")


def _login_to_server(url, pairing_code=None, device_label="TRAC CLI"):
    url = _validate_server_url(url)
    if pairing_code:
        response = requests.post(
            url + "/v1/pairings/claim",
            json={"code": pairing_code.strip(), "device_label": device_label},
            timeout=10,
        )
        if response.status_code != 200:
            raise ValueError("Pairing failed; check the code and server")
        credential = response.json()
        token = credential.get("access_token")
        identity = {
            "project_id": credential.get("project_id"),
            "scopes": credential.get("scopes"),
            "device_id": credential.get("device_id"),
        }
    else:
        pairing_code = getpass.getpass("One-time TRAC pairing code: ").strip()
        if not pairing_code:
            raise ValueError("A pairing code is required")
        return _login_to_server(
            url,
            pairing_code=pairing_code,
            device_label=device_label,
        )

    if (
        not isinstance(token, str)
        or not token
        or not isinstance(identity.get("project_id"), str)
        or not isinstance(identity.get("scopes"), list)
    ):
        raise ValueError("TRAC returned an invalid device credential")
    _save_login(url, token, identity)
    return identity


def _require_scope(config, scope):
    scopes = config.get("scopes")
    if not isinstance(scopes, list) or scope not in scopes:
        raise PermissionError(f"Device credential lacks {scope} permission")


def _api_get(path, *, timeout=10):
    config = _read_config()
    token = config.get("token")
    url = config.get("url")
    if not isinstance(token, str) or not token or not isinstance(url, str):
        raise PermissionError("Run 'trac login' first")
    _require_scope(config, "events:read")
    response = requests.get(
        url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if response.status_code == 401:
        state = _read_state()
        state["auth_error"] = True
        _write_state(state)
        raise PermissionError("TRAC credential is revoked or no longer valid")
    if response.status_code == 403:
        raise PermissionError("Device credential lacks permission")
    if response.status_code != 200:
        raise RuntimeError(f"TRAC request failed with HTTP {response.status_code}")
    return response.json()


def _is_uuid(value):
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _resolve_task(task_reference, tasks):
    try:
        normalized_uuid = str(uuid.UUID(task_reference))
    except (ValueError, AttributeError):
        normalized_uuid = None
    matches = [
        task
        for task in tasks
        if (normalized_uuid and task.get("id") == normalized_uuid)
        or task.get("title", "").casefold() == task_reference.casefold()
    ]
    if len(matches) != 1:
        raise ValueError(
            "Task must match one assigned task ID or an unambiguous exact title"
        )
    return matches[0]


def _build_canonical_task_event(event_type, task_id, metadata, project_id):
    if event_type not in TASK_EVENT_FIELDS:
        raise ValueError("Unsupported task event type")
    try:
        task_id = str(uuid.UUID(task_id))
        project_id = str(uuid.UUID(project_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Task and project IDs must be UUIDs") from exc
    if not isinstance(metadata, dict) or set(metadata) - TASK_EVENT_FIELDS[event_type]:
        raise ValueError("Task event metadata contains unsupported fields")
    clean_metadata = redact_value(metadata)
    if event_type == "task.progress":
        progress = clean_metadata.get("progress")
        if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
            raise ValueError("Progress must be an integer from 0 to 100")
    reason = clean_metadata.get("reason_code", "other")
    if event_type == "task.blocked" and reason not in TASK_BLOCK_REASONS:
        raise ValueError("Unsupported task blocker reason code")
    if event_type == "task.blocked":
        clean_metadata["reason_code"] = reason
    if event_type == "task.completed":
        if clean_metadata.get("outcome") not in {"done", "partial", "abandoned"}:
            raise ValueError("Unsupported task completion outcome")
    if "note" in clean_metadata:
        note = clean_metadata["note"]
        if not isinstance(note, str) or len(note) > 160 or "\n" in note or "\r" in note:
            raise ValueError("Task notes must be a single line of at most 160 characters")
        clean_metadata["note"] = note
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": 2,
        "event_type": event_type,
        "project_id": project_id,
        "task_id": task_id,
        "source": "cli",
        "occurred_at": _utc_now(),
        "metadata": clean_metadata,
    }


def _valid_queued_canonical_event(event, project_id):
    allowed_fields = {
        "event_id",
        "schema_version",
        "event_type",
        "project_id",
        "task_id",
        "source",
        "occurred_at",
        "metadata",
    }
    if not isinstance(event, dict) or set(event) != allowed_fields:
        return False
    if event.get("schema_version") != 2 or event.get("source") not in {
        "skill",
        "cli",
        "mcp",
        "github",
        "ci",
    }:
        return False
    if event.get("project_id") != project_id:
        return False
    try:
        uuid.UUID(event["event_id"])
        uuid.UUID(event["task_id"])
        parsed = dt.datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False
    event_type = event.get("event_type")
    metadata = event.get("metadata")
    if event_type not in TASK_EVENT_FIELDS or not isinstance(metadata, dict):
        return False
    if set(metadata) - TASK_EVENT_FIELDS[event_type]:
        return False
    try:
        if event_type == "task.progress":
            progress = metadata.get("progress")
            if isinstance(progress, bool) or not isinstance(progress, int):
                return False
            if not 0 <= progress <= 100:
                return False
        elif event_type == "task.blocked":
            if metadata.get("reason_code", "other") not in TASK_BLOCK_REASONS:
                return False
        elif event_type == "task.completed":
            if metadata.get("outcome") not in {"done", "partial", "abandoned"}:
                return False
        if "note" in metadata and (
            not isinstance(metadata["note"], str) or len(metadata["note"]) > 160
        ):
            return False
    except TypeError:
        return False
    return True


def _report_task_event(event_type, task_reference, *, percent=None, note=None, reason_code=None):
    config = _read_config()
    if not config.get("token") or not config.get("url"):
        raise PermissionError("Run 'trac login' first")
    _require_scope(config, "events:ingest")
    if (STATE_DIR / "paused").exists():
        print("Tracking is paused; no event was recorded.")
        return
    try:
        tasks = _api_get("/v1/me/tasks")
        task = _resolve_task(task_reference, tasks)
    except requests.RequestException:
        cached_ids = _read_state().get("assigned_task_ids", [])
        try:
            cached_task_id = str(uuid.UUID(task_reference))
        except (ValueError, AttributeError) as exc:
            raise RuntimeError(
                "TRAC is unavailable; use a task UUID from the last successful task list"
            ) from exc
        if cached_task_id not in cached_ids:
            raise RuntimeError(
                "TRAC is unavailable and this task was not in the last successful assigned-task list"
            )
        task = {"id": cached_task_id}
    metadata = {}
    if event_type == "task.progress":
        metadata["progress"] = percent
    elif event_type == "task.blocked":
        metadata["reason_code"] = reason_code or "other"
    elif event_type == "task.completed":
        metadata["outcome"] = "done"
    if note is not None and event_type != "task.started":
        metadata["note"] = note
    event = _build_canonical_task_event(
        event_type,
        task["id"],
        metadata,
        config["project_id"],
    )
    with _queue_lock():
        _append_bounded_queue([event])
    result = flush_queue()
    if result.get("auth_error"):
        print("Task event queued, but the device credential was revoked.")
    elif result.get("queued"):
        print(f"Task event queued ({result['queued']} pending).")
    else:
        print(f"Recorded {event_type} for task {task['id']}.")


def _tasks():
    tasks = _api_get("/v1/me/tasks")
    state = _read_state()
    state["assigned_task_ids"] = [
        task["id"]
        for task in tasks
        if isinstance(task, dict)
        and isinstance(task.get("id"), str)
        and _is_uuid(task["id"])
    ][:MAX_QUEUE_EVENTS]
    _write_state(state)
    if not tasks:
        print("No assigned tasks.")
        return
    for task in tasks:
        print(
            f"{task['id']}  {task['status']}  {task['priority']}  "
            f"{task['progress_percent']}%{' estimate' if task.get('progress_is_estimate') else ''}  "
            f"{task['title']}"
        )


def _install_skill(target):
    workspace = Path.cwd()
    source = Path(__file__).resolve().parents[2] / "skill" / "trac" / "SKILL.md"
    if not source.is_file():
        raise FileNotFoundError(
            "TRAC universal skill source is missing; install from a TRAC checkout"
        )
    target_dirs = {
        "claude": ".claude/skills",
        "cursor": ".cursor/skills",
        "codex": ".agents/skills",
        "copilot": ".github/skills",
        "generic": ".agents/skills",
    }
    if target == "auto":
        candidates = [
            (name, workspace / directory)
            for name, directory in target_dirs.items()
            if name != "generic" and (workspace / directory.split("/")[0]).is_dir()
        ]
        if len(candidates) > 1:
            candidate_names = ", ".join(name for name, _ in candidates)
            raise ValueError(
                f"Multiple agent environments detected ({candidate_names}); "
                "choose --target explicitly"
            )
        target = candidates[0][0] if candidates else "generic"
    destination = workspace / target_dirs[target] / "trac" / "SKILL.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = source.read_bytes()
    if destination.exists():
        if destination.read_bytes() != content:
            raise FileExistsError(
                f"Existing skill was not overwritten: {destination}"
            )
        return destination
    destination.write_bytes(content)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination


def _disconnect():
    config = _read_config()
    token = config.get("token")
    url = config.get("url")
    if not isinstance(token, str) or not token or not isinstance(url, str):
        _logout()
        return
    try:
        response = requests.delete(
            url.rstrip("/") + "/v1/me/device",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Could not reach TRAC; credentials were kept locally") from exc
    if response.status_code not in {204, 401}:
        raise RuntimeError(
            f"Could not revoke device credential (HTTP {response.status_code})"
        )
    with _queue_lock():
        pending = _read_jsonl(STATE_DIR / "queue.jsonl")
        if pending:
            state = _read_json(STATE_DIR / "state.json", {})
            state["discarded_on_disconnect"] = (
                state.get("discarded_on_disconnect", 0) + len(pending)
            )
            _write_json(STATE_DIR / "state.json", state)
        _write_jsonl(STATE_DIR / "queue.jsonl", [])
    _logout()
    print("Device disconnected; server credential revoked and pending events cleared.")


def _status():
    state = _read_state()
    queued = _queue_size()
    paused = (STATE_DIR / "paused").exists()
    config = _read_config()
    print(f"Tracking: {'paused' if paused else 'on'}")
    print(f"Login: {'logged in' if config.get('token') else 'not logged in'}")
    authentication = "unknown"
    if config.get("token") and config.get("url"):
        try:
            response = requests.get(
                config["url"].rstrip("/") + "/v1/me/device",
                headers={"Authorization": f"Bearer {config['token']}"},
                timeout=5,
            )
            authentication = (
                "ok" if response.status_code == 200 else "revoked or unauthorized"
            )
        except requests.RequestException:
            authentication = "server unavailable"
    elif state.get("auth_error"):
        authentication = "revoked or unauthorized"
    print(f"Authentication: {authentication}")
    print(f"Queued: {queued}")
    print(f"Dropped (queue limit): {state.get('dropped_queue_events', 0)}")
    print(f"Last successful send: {state.get('last_success_at', 'never')}")


def _print_data():
    queued = _read_jsonl(STATE_DIR / "queue.jsonl")
    sent = _prune_sent_records()
    print(json.dumps({"queued": queued, "sent": sent}, indent=2, ensure_ascii=True))


def _save_login(url, token, identity=None):
    _ensure_state_dir()
    credential_store = _store_credential(token)
    config = _read_config()
    config.pop("token", None)
    config.update({"url": url.rstrip("/")})
    if identity:
        config.update(
            {
                "project_id": identity.get("project_id"),
                "scopes": identity.get("scopes", []),
                "device_id": identity.get("device_id"),
                "credential_store": credential_store,
            }
        )
    elif "scopes" not in config:
        config["scopes"] = ["events:read", "events:ingest"]
        config["credential_store"] = credential_store
    _write_config(config)
    state = _read_state()
    state.pop("auth_error", None)
    _write_state(state)


def _logout():
    config = _read_config()
    _delete_credential()
    config.pop("token", None)
    config.pop("project_id", None)
    config.pop("scopes", None)
    config.pop("device_id", None)
    config.pop("credential_store", None)
    if config.get("url"):
        _write_config(config)
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
    login.add_argument("--url")
    login.add_argument("--device-label", default="TRAC CLI")
    login.add_argument(
        "--pairing-code-stdin",
        action="store_true",
        help="read the one-time pairing code from standard input without echoing",
    )

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
    subparsers.add_parser("tasks")
    task = subparsers.add_parser("task")
    task_subparsers = task.add_subparsers(dest="task_command", required=True)
    task_start = task_subparsers.add_parser("start")
    task_start.add_argument("task")
    task_progress = task_subparsers.add_parser("progress")
    task_progress.add_argument("task")
    task_progress.add_argument("--percent", required=True, type=int)
    task_progress.add_argument("--note")
    task_block = task_subparsers.add_parser("block")
    task_block.add_argument("task")
    task_block.add_argument(
        "--reason-code", choices=sorted(TASK_BLOCK_REASONS), default="other"
    )
    task_block.add_argument("--note")
    task_done = task_subparsers.add_parser("done")
    task_done.add_argument("task")
    task_done.add_argument("--note")
    subparsers.add_parser("pause")
    subparsers.add_parser("resume")
    subparsers.add_parser("data")
    subparsers.add_parser("disconnect")
    subparsers.add_parser("logout")
    init = subparsers.add_parser("init")
    init.add_argument(
        "--target",
        choices=("auto", "claude", "cursor", "codex", "copilot", "generic"),
        default="auto",
    )
    init.add_argument("--git-hooks", action="store_true")
    init.add_argument("--claude", action="store_true", help=argparse.SUPPRESS)
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
            if not args.url:
                parser.error("login requires --url")
            pairing_code = None
            if args.pairing_code_stdin:
                pairing_code = sys.stdin.readline().rstrip("\r\n")
                if not pairing_code:
                    raise ValueError("No one-time pairing code was provided on stdin")
            _login_to_server(
                args.url,
                pairing_code=pairing_code,
                device_label=args.device_label,
            )
            print("Logged in with a project-scoped device credential.")
        elif args.command == "tasks":
            _tasks()
        elif args.command == "task":
            event_types = {
                "start": "task.started",
                "progress": "task.progress",
                "block": "task.blocked",
                "done": "task.completed",
            }
            _report_task_event(
                event_types[args.task_command],
                args.task,
                percent=getattr(args, "percent", None),
                note=getattr(args, "note", None),
                reason_code=getattr(args, "reason_code", None),
            )
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
        elif args.command == "disconnect":
            _disconnect()
        elif args.command == "logout":
            _logout()
            print("Local credentials removed; server credential was not revoked.")
        elif args.command == "init":
            skill_path = _install_skill(args.target)
            print(f"Installed the universal TRAC skill at {skill_path}")
            if args.git_hooks or args.claude:
                print(f"Installed optional Git hooks in {init_hooks()}")
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
