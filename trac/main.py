import asyncio
import hashlib
import json
import math
import re
import threading
import time
from collections import deque
from datetime import timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, List, Optional, Type
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from trac.db import get_connection, init_db
from trac.models import (
    AgentPromptSubmittedData,
    CanonicalEvent,
    CommandRunData,
    Event,
    EventCreate,
    FileEditedData,
    GitBranchData,
    GitCommitData,
    GitPushData,
    SessionActivityData,
    TaskActivityData,
    VersionedEvent,
    now_iso,
)

app = FastAPI(title="TRAC — Engineering Intelligence Layer")

STATIC_DIR = Path(__file__).parent / "static"
MAX_ACTIVITY_BODY_BYTES = 256 * 1024
MAX_ACTIVITY_BATCH_SIZE = 100
RATE_LIMIT_REQUESTS = 120
RATE_LIMIT_WINDOW_SECONDS = 60
STREAM_POLL_SECONDS = 0.5
STREAM_HEARTBEAT_SECONDS = 15
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)\b(?:password|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
ACTIVITY_DATA_MODELS: Dict[str, Type[BaseModel]] = {
    "session.start": SessionActivityData,
    "session.end": SessionActivityData,
    "task.started": TaskActivityData,
    "task.completed": TaskActivityData,
    "task.blocked": TaskActivityData,
    "file.edited": FileEditedData,
    "command.run": CommandRunData,
    "agent.prompt_submitted": AgentPromptSubmittedData,
    "git.commit": GitCommitData,
    "git.push": GitPushData,
    "git.branch": GitBranchData,
}
_rate_limit_hits: Dict[int, deque[float]] = {}
_rate_limit_lock = threading.Lock()


@app.on_event("startup")
def on_startup():
    init_db()


def _event_response(row):
    event = dict(row)
    event["metadata"] = json.loads(event["metadata"] or "{}")
    return event


def _authenticate_device(authorization: Optional[str], scope: str):
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")

    token_hash = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT devices.id AS device_id, devices.user_id, devices.project_id,
                   devices.scopes, users.public_id AS actor_id,
                   users.github_login, users.display_name, projects.slug AS project_slug
            FROM devices
            JOIN users ON users.id = devices.user_id
            JOIN projects ON projects.id = devices.project_id
            JOIN project_memberships
              ON project_memberships.project_id = devices.project_id
             AND project_memberships.user_id = devices.user_id
            WHERE devices.token_hash = ? AND devices.revoked_at IS NULL
            """,
            (token_hash,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")

    try:
        scopes = json.loads(row["scopes"])
    except (TypeError, JSONDecodeError):
        scopes = []
    if scope not in scopes:
        raise HTTPException(
            status_code=403, detail="Credential lacks required permission"
        )
    return row


def _device_is_active(
    device_id: int, user_id: int, project_id: str, scope: str
) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT devices.scopes FROM devices
            JOIN project_memberships
              ON project_memberships.project_id = devices.project_id
             AND project_memberships.user_id = devices.user_id
            WHERE devices.id = ? AND devices.user_id = ? AND devices.project_id = ?
              AND devices.revoked_at IS NULL
            """,
            (device_id, user_id, project_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    try:
        return scope in json.loads(row["scopes"])
    except (TypeError, JSONDecodeError):
        return False


def _check_rate_limit(device_id: int) -> Optional[int]:
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    with _rate_limit_lock:
        hits = _rate_limit_hits.setdefault(device_id, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_REQUESTS:
            return max(1, math.ceil(RATE_LIMIT_WINDOW_SECONDS - (now - hits[0])))
        hits.append(now)
    return None


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_ACTIVITY_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Request body is too large")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length"
            ) from exc

    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ACTIVITY_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _reject_sensitive_values(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str):
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                forbidden_tokens = {
                    "content",
                    "diff",
                    "prompt",
                    "text",
                    "command",
                    "env",
                    "secret",
                    "credential",
                    "token",
                    "authorization",
                    "password",
                    "apikey",
                    "accesskey",
                    "privatekey",
                    "auth",
                    "key",
                }
                if (
                    "code" in normalized
                    and normalized not in {"exitcode", "reasoncode"}
                ) or any(token in normalized for token in forbidden_tokens):
                    raise ValueError("sensitive_field")
            _reject_sensitive_values(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_values(child)
    elif isinstance(value, str) and any(
        pattern.search(value) for pattern in SECRET_PATTERNS
    ):
        raise ValueError("sensitive_value")


def _activity_summary(event_type: str, data: Dict[str, Any]) -> str:
    summaries = {
        "task.started": "Started a task",
        "task.progress": f"Updated task progress ({data.get('progress', 0)}%)",
        "task.blocked": "Blocked a task",
        "task.completed": "Completed a task",
        "git.commit": "Committed changes",
        "git.pull_request.opened": "Opened a pull request",
        "git.pull_request.merged": "Merged a pull request",
        "test.completed": f"Tests {data.get('result', 'completed')}",
        "build.completed": f"Build {data.get('result', 'completed')}",
        "session.started": "Started a session",
        "session.ended": "Ended a session",
        "session.start": "Started a session",
        "session.end": "Ended a session",
        "git.push": "Pushed changes",
        "git.branch": "Updated a branch",
    }
    if event_type == "git.commit" and data.get("files_changed") is not None:
        additions = data.get("insertions", data.get("lines_added", 0))
        deletions = data.get("deletions", data.get("lines_removed", 0))
        return (
            f"Committed {data['files_changed']} files "
            f"(+{additions or 0} -{deletions or 0})"
        )
    if event_type == "command.run" and data.get("kind") == "test":
        exit_code = data.get("exit_code")
        return (
            f"Test run failed (exit code {exit_code})"
            if exit_code not in (None, 0)
            else "Ran tests"
        )
    if event_type == "task.started" and data.get("summary"):
        return f"Started task: {data['summary']}"
    if event_type == "task.completed" and data.get("summary"):
        return f"Completed task: {data['summary']}"
    if event_type == "task.blocked" and data.get("reason"):
        return f"Blocked task: {data['reason']}"
    return summaries.get(event_type, f"Recorded {event_type.replace('.', ' ')}")


def _insert_dashboard_mirror(
    conn,
    *,
    event_id: str,
    event_type: str,
    source: str,
    data: Dict[str, Any],
    project_id: str,
    device,
    occurred_at: str,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
):
    status = None
    if event_type in {"command.run", "test.completed", "build.completed"}:
        success = (
            data.get("exit_code") == 0
            if event_type == "command.run"
            else data.get("result") == "passed"
        )
        status = "success" if success else "failed"
    conn.execute(
        """
        INSERT OR IGNORE INTO events
            (type, source, summary, repo, ref, actor, timestamp, status, metadata,
             project_id, actor_user_id, activity_event_id)
        VALUES (?, 'trac-activity', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"activity.{event_type}",
            _activity_summary(event_type, data)[:200],
            repo,
            branch,
            device["display_name"] or device["github_login"],
            occurred_at,
            status,
            json.dumps(data),
            project_id,
            device["user_id"],
            event_id,
        ),
    )


def _canonical_response(conn, event_id: str, project_id: str):
    row = conn.execute(
        """
        SELECT activity_events.event_id, activity_events.schema_version,
               activity_events.type AS event_type, activity_events.project_id,
               activity_events.task_id, users.public_id AS actor_id,
               activity_events.source, activity_events.ts AS occurred_at,
               activity_events.received_at, activity_events.data AS metadata
        FROM activity_events
        JOIN users ON users.id = activity_events.user_id
        WHERE activity_events.event_id = ? AND activity_events.project_id = ?
          AND activity_events.source != 'compat-api'
        """,
        (event_id, project_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["metadata"] = json.loads(result["metadata"] or "{}")
    if result["source"] not in {"skill", "cli", "mcp", "github", "ci"}:
        result["source"] = (
            "cli" if result["source"] in {"git-hook", "claude-hook"} else "skill"
        )
    return result


def _canonical_event_from_input(raw_event: Any):
    event = CanonicalEvent.model_validate(raw_event)
    return event


def _legacy_source(source: Optional[str]) -> str:
    if source in {"skill", "cli", "mcp", "github", "ci"}:
        return source
    if source in {"git-hook", "claude-hook", "terminal"}:
        return "cli"
    return "skill"


def _compatibility_source(source: Optional[str]) -> str:
    if source in {"skill", "cli", "mcp", "github", "ci"}:
        return source
    if source == "code-review-agent" or (source and "github" in source.lower()):
        return "github"
    return "cli"


def _ingest_canonical(conn, event: CanonicalEvent, device, received_at: str):
    event_id = str(event.event_id)
    existing = conn.execute(
        "SELECT project_id FROM activity_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if existing is not None:
        if existing["project_id"] != device["project_id"]:
            raise HTTPException(status_code=409, detail="Event ID already exists")
        return False

    data = event.metadata
    conn.execute(
        """
        INSERT INTO activity_events
            (event_id, schema_version, type, ts, received_at, user_id, repo, branch,
             source, data, project_id, task_id)
        VALUES (?, 2, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
        """,
        (
            event_id,
            event.event_type,
            event.occurred_at.astimezone(timezone.utc).isoformat(),
            received_at,
            device["user_id"],
            event.source,
            json.dumps(data),
            device["project_id"],
            str(event.task_id) if event.task_id else None,
        ),
    )
    conn.execute(
        """
        INSERT INTO activity_event_stream (event_id, project_id, created_at)
        VALUES (?, ?, ?)
        """,
        (event_id, device["project_id"], received_at),
    )
    _insert_dashboard_mirror(
        conn,
        event_id=event_id,
        event_type=event.event_type,
        source=event.source,
        data=data,
        project_id=device["project_id"],
        device=device,
        occurred_at=received_at,
    )
    return True


def _ingest_legacy(conn, event: VersionedEvent, device, received_at: str):
    data_model = ACTIVITY_DATA_MODELS[event.type].model_validate(event.data)
    data = data_model.model_dump(exclude_none=True)
    _reject_sensitive_values(data)
    event_id = event.event_id
    existing = conn.execute(
        "SELECT project_id FROM activity_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if existing is not None:
        if existing["project_id"] != device["project_id"]:
            raise HTTPException(status_code=409, detail="Event ID already exists")
        return False

    conn.execute(
        """
        INSERT INTO activity_events
            (event_id, schema_version, type, ts, received_at, session_id, user_id,
             repo, branch, source, tool, data, project_id)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event.type,
            event.ts.astimezone(timezone.utc).isoformat(),
            received_at,
            event.session_id,
            device["user_id"],
            event.repo,
            event.branch,
            _legacy_source(event.source),
            event.tool,
            json.dumps(data),
            device["project_id"],
        ),
    )
    conn.execute(
        """
        INSERT INTO activity_event_stream (event_id, project_id, created_at)
        VALUES (?, ?, ?)
        """,
        (event_id, device["project_id"], received_at),
    )
    mirrorable = event.type.startswith(("task.", "git.", "session.")) or (
        event.type == "command.run" and data.get("kind") == "test"
    )
    if mirrorable:
        _insert_dashboard_mirror(
            conn,
            event_id=event_id,
            event_type=event.type,
            source=event.source or "skill",
            data=data,
            project_id=device["project_id"],
            device=device,
            occurred_at=received_at,
            repo=event.repo,
            branch=event.branch,
        )
    return True


@app.post("/v1/events", status_code=202)
async def create_activity_events(request: Request):
    device = _authenticate_device(request.headers.get("authorization"), "events:ingest")
    retry_after = _check_rate_limit(device["device_id"])
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )

    body = await _read_limited_body(request)
    try:
        payload = json.loads(body)
    except (JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=422, detail="Request body must be valid JSON"
        ) from exc

    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        raw_events = payload["events"]
    elif isinstance(payload, dict):
        raw_events = [payload]
    else:
        raise HTTPException(
            status_code=422, detail="Body must be an event or events array"
        )
    if len(raw_events) > MAX_ACTIVITY_BATCH_SIZE:
        raise HTTPException(
            status_code=413, detail="A batch may contain at most 100 events"
        )

    accepted = 0
    duplicates = 0
    rejected = []
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for raw_event in raw_events:
            raw_event_id = (
                raw_event.get("event_id") if isinstance(raw_event, dict) else None
            )
            try:
                if isinstance(raw_event, dict) and raw_event.get("schema_version") == 2:
                    event = _canonical_event_from_input(raw_event)
                    _reject_sensitive_values(event.metadata)
                    inserted = _ingest_canonical(conn, event, device, now_iso())
                else:
                    event = VersionedEvent.model_validate(raw_event)
                    _reject_sensitive_values(event.data)
                    inserted = _ingest_legacy(conn, event, device, now_iso())
            except HTTPException:
                raise
            except (ValidationError, TypeError, ValueError, KeyError) as exc:
                if isinstance(raw_event, dict) and raw_event.get("schema_version") == 2:
                    raise HTTPException(
                        status_code=422,
                        detail="Invalid event schema or disallowed metadata",
                    ) from exc
                rejected.append(
                    {
                        "event_id": raw_event_id,
                        "reason": "forbidden_field"
                        if str(exc) in {"sensitive_field", "sensitive_value"}
                        else "invalid_event",
                    }
                )
                continue
            if inserted:
                accepted += 1
            else:
                duplicates += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


@app.get("/v1/events/stream")
@app.get("/api/events/stream")
async def stream_activity_events(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
):
    device = _authenticate_device(authorization, "events:read")
    conn = get_connection()
    try:
        if last_event_id == "0":
            initial_sequence = 0
        elif last_event_id:
            cursor = conn.execute(
                """
                SELECT sequence FROM activity_event_stream
                WHERE event_id = ? AND project_id = ?
                """,
                (last_event_id, device["project_id"]),
            ).fetchone()
            if cursor is None:
                raise HTTPException(
                    status_code=400, detail="Invalid Last-Event-ID cursor"
                )
            initial_sequence = cursor["sequence"]
        else:
            initial_sequence = conn.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) FROM activity_event_stream
                WHERE project_id = ?
                """,
                (device["project_id"],),
            ).fetchone()[0]
    finally:
        conn.close()

    async def event_stream():
        sequence = initial_sequence
        yield "retry: 3000\n\n"
        heartbeat_at = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            if not _device_is_active(
                device["device_id"],
                device["user_id"],
                device["project_id"],
                "events:read",
            ):
                return
            conn = get_connection()
            try:
                rows = conn.execute(
                    """
                    SELECT activity_event_stream.sequence, activity_events.event_id,
                           activity_events.schema_version,
                           activity_events.type AS event_type,
                           activity_events.project_id, activity_events.task_id,
                           users.public_id AS actor_id, activity_events.source,
                           activity_events.tool,
                           activity_events.ts AS occurred_at,
                           activity_events.received_at,
                           activity_events.data AS metadata
                    FROM activity_event_stream
                    JOIN activity_events
                      ON activity_events.event_id = activity_event_stream.event_id
                    JOIN users ON users.id = activity_events.user_id
                    WHERE activity_event_stream.project_id = ?
                      AND activity_event_stream.sequence > ?
                    ORDER BY activity_event_stream.sequence LIMIT 100
                    """,
                    (device["project_id"], sequence),
                ).fetchall()
            finally:
                conn.close()
            if rows:
                for row in rows:
                    sequence = row["sequence"]
                    result = dict(row)
                    result.pop("sequence", None)
                    frame_type = "activity"
                    if result["source"] == "compat-api":
                        frame_type = "compatibility"
                        result["source"] = _compatibility_source(
                            result.pop("tool", None)
                        )
                    elif result["source"] not in {
                        "skill",
                        "cli",
                        "mcp",
                        "github",
                        "ci",
                    }:
                        result["source"] = (
                            "cli"
                            if result["source"] in {"git-hook", "claude-hook"}
                            else "skill"
                        )
                    result["metadata"] = json.loads(result["metadata"] or "{}")
                    yield (
                        f"id: {result['event_id']}\n"
                        f"event: {frame_type}\n"
                        f"data: {json.dumps(result, separators=(',', ':'))}\n\n"
                    )
                heartbeat_at = time.monotonic()
            else:
                if time.monotonic() - heartbeat_at >= STREAM_HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    heartbeat_at = time.monotonic()
                await asyncio.sleep(STREAM_POLL_SECONDS)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/v1/events")
def list_activity_events(
    response: Response,
    authorization: Optional[str] = Header(default=None),
    limit: int = 100,
):
    device = _authenticate_device(authorization, "events:read")
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        rows = conn.execute(
            """
            SELECT event_id FROM activity_events
            WHERE project_id = ? AND source != 'compat-api'
            ORDER BY received_at DESC LIMIT ?
            """,
            (device["project_id"], limit),
        ).fetchall()
        events = [
            _canonical_response(conn, row["event_id"], device["project_id"])
            for row in rows
        ]
        cursor = conn.execute(
            """
            SELECT event_id FROM activity_event_stream
            WHERE project_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (device["project_id"],),
        ).fetchone()
        response.headers["X-TRAC-Event-Cursor"] = cursor["event_id"] if cursor else "0"
        conn.commit()
    finally:
        conn.close()
    return events


@app.get("/v1/events/{event_id}")
def get_activity_event(
    event_id: str, authorization: Optional[str] = Header(default=None)
):
    device = _authenticate_device(authorization, "events:read")
    try:
        event_id = str(UUID(event_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    conn = get_connection()
    try:
        event = _canonical_response(conn, event_id, device["project_id"])
    finally:
        conn.close()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


def _validate_legacy_payload(event: EventCreate):
    payload = event.model_dump()
    try:
        _reject_sensitive_values(
            {
                key: payload.get(key)
                for key in ("type", "source", "summary", "repo", "ref", "execution_id")
            }
        )
        _reject_sensitive_values(payload.get("metadata") or {})
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 16 * 1024:
            raise ValueError("payload_too_large")
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Disallowed sensitive event data"
        ) from exc


def _insert_compatibility_stream_event(
    conn, event: EventCreate, device, timestamp: str, row_id: int
):
    event_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO activity_events
            (event_id, schema_version, type, ts, received_at, user_id, repo, branch,
             source, tool, data, project_id)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'compat-api', ?, ?, ?)
        """,
        (
            event_id,
            event.type,
            timestamp,
            timestamp,
            device["user_id"],
            event.repo,
            event.ref,
            event.source,
            json.dumps(event.metadata),
            device["project_id"],
        ),
    )
    conn.execute(
        """
        INSERT INTO activity_event_stream (event_id, project_id, created_at)
        VALUES (?, ?, ?)
        """,
        (event_id, device["project_id"], timestamp),
    )
    conn.execute(
        "UPDATE events SET activity_event_id = ? WHERE id = ?",
        (event_id, row_id),
    )


@app.post("/api/events", response_model=Event)
def create_event(event: EventCreate, request: Request):
    device = _authenticate_device(request.headers.get("authorization"), "events:ingest")
    _validate_legacy_payload(event)
    conn = get_connection()
    timestamp = now_iso()
    metadata_json = json.dumps(event.metadata)
    values = (
        event.type,
        event.source,
        event.summary,
        event.repo,
        event.ref,
        device["display_name"] or device["github_login"],
        timestamp,
        event.execution_id,
        event.status,
        metadata_json,
        device["project_id"],
        device["user_id"],
    )
    unchanged_execution = False
    try:
        if event.execution_id:
            existing = conn.execute(
                "SELECT * FROM events WHERE project_id = ? AND execution_id = ?",
                (device["project_id"], event.execution_id),
            ).fetchone()
            unchanged_execution = existing is not None and all(
                existing[key] == expected
                for key, expected in {
                    "type": event.type,
                    "source": event.source,
                    "summary": event.summary,
                    "repo": event.repo,
                    "ref": event.ref,
                    "status": event.status,
                }.items()
            )
            if unchanged_execution:
                unchanged_execution = (
                    json.loads(existing["metadata"] or "{}") == event.metadata
                )
            conn.execute(
                """
                INSERT INTO events
                    (
                        type, source, summary, repo, ref, actor, timestamp,
                        execution_id, status, metadata, project_id, actor_user_id
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, execution_id)
                WHERE execution_id IS NOT NULL DO UPDATE SET
                    type = excluded.type, source = excluded.source,
                    summary = excluded.summary,
                    repo = excluded.repo, ref = excluded.ref, actor = excluded.actor,
                    status = excluded.status, metadata = excluded.metadata,
                    actor_user_id = excluded.actor_user_id
                """,
                values,
            )
            row = conn.execute(
                "SELECT * FROM events WHERE project_id = ? AND execution_id = ?",
                (device["project_id"], event.execution_id),
            ).fetchone()
        else:
            cursor = conn.execute(
                """
                INSERT INTO events
                    (
                        type, source, summary, repo, ref, actor, timestamp,
                        execution_id, status, metadata, project_id, actor_user_id
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = conn.execute(
                "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        if not unchanged_execution:
            _insert_compatibility_stream_event(
                conn, event, device, timestamp, row["id"]
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _event_response(row)


@app.get("/api/events", response_model=List[Event])
def list_events(
    request: Request,
    response: Response,
    source: Optional[str] = None,
    limit: int = 100,
):
    device = _authenticate_device(request.headers.get("authorization"), "events:read")
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        if source:
            rows = conn.execute(
                """
                SELECT * FROM events WHERE project_id = ? AND source = ?
                ORDER BY timestamp DESC LIMIT ?
                """,
                (device["project_id"], source, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM events WHERE project_id = ?
                ORDER BY timestamp DESC LIMIT ?
                """,
                (device["project_id"], limit),
            ).fetchall()
        cursor = conn.execute(
            """
            SELECT event_id FROM activity_event_stream
            WHERE project_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (device["project_id"],),
        ).fetchone()
        response.headers["X-TRAC-Event-Cursor"] = cursor["event_id"] if cursor else "0"
        conn.commit()
    finally:
        conn.close()
    return [_event_response(row) for row in rows]


@app.get("/api/events/{event_id}", response_model=Event)
def get_event(event_id: int, request: Request):
    device = _authenticate_device(request.headers.get("authorization"), "events:read")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM events WHERE id = ? AND project_id = ?",
            (event_id, device["project_id"]),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_response(row)


@app.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/legacy")
def legacy_dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
