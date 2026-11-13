import hashlib
import json
import math
import threading
import time
from collections import deque
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from trac.db import get_connection, init_db
from trac.models import (
    AgentPromptSubmittedData,
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
FORBIDDEN_DATA_FIELDS = {
    "content", "code", "diff", "prompt", "text", "command", "env", "secret",
}
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


# ---------- Ingest an event ----------

@app.post("/api/events", response_model=Event)
def create_event(event: EventCreate):
    conn = get_connection()
    timestamp = now_iso()
    metadata_json = json.dumps(event.metadata)

    values = (
        event.type, event.source, event.summary, event.repo, event.ref, event.actor,
        timestamp, event.execution_id, event.status, metadata_json,
    )
    if event.execution_id:
        conn.execute(
            """
            INSERT INTO events
                (type, source, summary, repo, ref, actor, timestamp, execution_id, status, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(execution_id) WHERE execution_id IS NOT NULL DO UPDATE SET
                type = excluded.type,
                source = excluded.source,
                summary = excluded.summary,
                repo = excluded.repo,
                ref = excluded.ref,
                actor = excluded.actor,
                status = excluded.status,
                metadata = excluded.metadata
            """,
            values,
        )
        row = conn.execute(
            "SELECT * FROM events WHERE execution_id = ?", (event.execution_id,)
        ).fetchone()
    else:
        cursor = conn.execute(
            """
            INSERT INTO events
                (type, source, summary, repo, ref, actor, timestamp, execution_id, status, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        row = conn.execute(
            "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    conn.commit()
    conn.close()

    return _event_response(row)


def _authenticate_device(authorization: Optional[str]):
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")

    token_hash = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    conn = get_connection()
    row = conn.execute(
        """
        SELECT devices.id AS device_id, users.id AS user_id,
               users.github_login, users.display_name
        FROM devices
        JOIN users ON users.id = devices.user_id
        WHERE devices.token_hash = ? AND devices.revoked_at IS NULL
        """,
        (token_hash,),
    ).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
    return row


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
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc

    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ACTIVITY_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _activity_summary(event: VersionedEvent, data: Dict[str, Any]) -> str:
    event_type = event.type
    if event_type == "git.commit":
        files_changed = data.get("files_changed")
        lines_added = data.get("lines_added")
        lines_removed = data.get("lines_removed")
        if files_changed is not None:
            return (
                f"Committed {files_changed} files "
                f"(+{lines_added or 0} -{lines_removed or 0})"
            )
        return "Committed changes"
    if event_type == "git.push":
        commits = data.get("commits")
        remote_branch = data.get("remote_branch")
        if commits is not None and remote_branch:
            return f"Pushed {commits} commits to {remote_branch}"
        if remote_branch:
            return f"Pushed changes to {remote_branch}"
        return "Pushed changes"
    if event_type == "git.branch":
        action = data.get("action") or "Updated"
        name = data.get("name")
        return f"{action.capitalize()} branch {name}" if name else f"{action.capitalize()} a branch"
    if event_type.startswith("task."):
        verb = {
            "task.started": "Started",
            "task.completed": "Completed",
            "task.blocked": "Blocked",
        }[event_type]
        detail = data.get("summary") or data.get("reason")
        return f"{verb} task: {detail}" if detail else f"{verb} a task"
    if event_type == "session.start":
        return "Started a session"
    if event_type == "session.end":
        return "Ended a session"
    if event_type == "command.run" and data.get("kind") == "test":
        exit_code = data.get("exit_code")
        if exit_code is not None and exit_code != 0:
            return f"Test run failed (exit code {exit_code})"
        return "Ran tests"
    return f"Recorded {event_type.replace('.', ' ')}"


def _should_mirror(event: VersionedEvent, data: Dict[str, Any]) -> bool:
    return (
        event.type.startswith(("task.", "git.", "session."))
        or (event.type == "command.run" and data.get("kind") == "test")
    )


@app.post("/v1/events", status_code=202)
async def create_activity_events(request: Request):
    device = _authenticate_device(request.headers.get("authorization"))
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
        raise HTTPException(status_code=422, detail="Request body must be valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise HTTPException(status_code=422, detail="Body must contain an events array")
    if len(payload["events"]) > MAX_ACTIVITY_BATCH_SIZE:
        raise HTTPException(status_code=413, detail="A batch may contain at most 100 events")

    accepted = 0
    duplicates = 0
    rejected = []
    conn = get_connection()
    try:
        for raw_event in payload["events"]:
            raw_event_id = (
                raw_event.get("event_id") if isinstance(raw_event, dict) else None
            )
            raw_data = raw_event.get("data") if isinstance(raw_event, dict) else None
            if isinstance(raw_data, dict) and any(
                isinstance(key, str) and key.lower() in FORBIDDEN_DATA_FIELDS
                for key in raw_data
            ):
                rejected.append({"event_id": raw_event_id, "reason": "forbidden_field"})
                continue

            try:
                event = VersionedEvent.model_validate(raw_event)
                data_model = ACTIVITY_DATA_MODELS[event.type].model_validate(event.data)
            except (ValidationError, TypeError, ValueError):
                rejected.append({"event_id": raw_event_id, "reason": "invalid_event"})
                continue

            data = data_model.model_dump(exclude_none=True)
            received_at = now_iso()
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO activity_events
                    (event_id, schema_version, type, ts, received_at, session_id,
                     user_id, repo, branch, source, tool, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.schema_version,
                    event.type,
                    event.ts.isoformat(),
                    received_at,
                    event.session_id,
                    device["user_id"],
                    event.repo,
                    event.branch,
                    event.source,
                    event.tool,
                    json.dumps(data),
                ),
            )
            if cursor.rowcount == 0:
                duplicates += 1
                continue

            accepted += 1
            if _should_mirror(event, data):
                actor = device["display_name"] or device["github_login"]
                status = None
                metadata = {}
                if event.type == "command.run":
                    metadata = data
                    exit_code = data.get("exit_code")
                    if data.get("kind") == "test" and exit_code is not None:
                        status = "success" if exit_code == 0 else "failed"
                conn.execute(
                    """
                    INSERT INTO events
                        (type, source, summary, repo, ref, actor, timestamp, status, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"activity.{event.type}",
                        "trac-activity",
                        _activity_summary(event, data)[:200],
                        event.repo,
                        event.branch,
                        actor,
                        received_at,
                        status,
                        json.dumps(metadata),
                    ),
                )
        conn.commit()
    finally:
        conn.close()

    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": rejected,
    }


def _event_response(row):
    event = dict(row)
    event["metadata"] = json.loads(event["metadata"] or "{}")
    return event


# ---------- List events (powers the timeline) ----------

@app.get("/api/events", response_model=List[Event])
def list_events(source: Optional[str] = None, limit: int = 100):
    conn = get_connection()

    if source:
        rows = conn.execute(
            "SELECT * FROM events WHERE source = ? ORDER BY timestamp DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()

    conn.close()
    return [_event_response(row) for row in rows]


@app.get("/api/events/stream")
def stream_events():
    def event_stream():
        yield "event: ping\ndata: {}\n\n"
        while True:
            yield "event: heartbeat\ndata: {\"ok\": true}\n\n"
            time.sleep(15)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- Get a single event ----------

@app.get("/api/events/{event_id}", response_model=Event)
def get_event(event_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_response(row)


# ---------- Serve the dashboard ----------

@app.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/legacy")
def legacy_dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")