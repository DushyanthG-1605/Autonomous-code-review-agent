import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, List, Optional, Type
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from trac.db import get_connection, init_db
from trac.models import (
    AgentPromptSubmittedData,
    CanonicalEvent,
    CommandRunData,
    DevLoginRequest,
    Event,
    EventCreate,
    FileEditedData,
    GitBranchData,
    GitCommitData,
    GitPushData,
    PairingClaim,
    PairingCreate,
    ProjectCreate,
    SessionActivityData,
    TaskActivityData,
    TaskCreate,
    TaskPatch,
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
    re.compile(r"(?i)\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)\b(?:password|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
FORBIDDEN_PAYLOAD_KEYS = {"content", "code", "diff", "prompt", "command", "env", "secret"}
NEW_EVENT_KEYS = {"task", "reason", "confidence", "occurredAt", "externalId", "tool", "payload", "engineer"}
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


def _development_auth_enabled() -> bool:
    return (
        os.environ.get("TRAC_ENV", "").strip().lower() == "development"
        and os.environ.get("TRAC_DEV_AUTH_ENABLED", "").strip().lower() == "true"
    )


def _is_project_head(device) -> bool:
    return (
        device["project_head_id"] == device["user_id"]
        or device["membership_role"] == "owner"
    )


def _require_project_device(device, project_id: str):
    if str(device["project_id"]) != str(project_id):
        raise HTTPException(status_code=404, detail="Project not found")


def _event_visibility_clause(device, alias="activity_events"):
    if _is_project_head(device):
        return "", ()
    return (
        f""" AND (
            {alias}.user_id = ?
            OR EXISTS (
                SELECT 1 FROM tasks visible_task
                WHERE visible_task.id = {alias}.task_id
                  AND visible_task.assignee_id = ?
            )
        )""",
        (device["user_id"], device["user_id"]),
    )


def _task_row(conn, task_id: str):
    return conn.execute(
        """
        SELECT tasks.*, projects.project_head_id,
               assignee.public_id AS assignee_public_id,
               assignee.display_name AS assignee_display_name,
               assignee.github_username AS assignee_github_username
        FROM tasks
        JOIN projects ON projects.id = tasks.project_id
        LEFT JOIN users AS assignee ON assignee.id = tasks.assignee_id
        WHERE tasks.id = ?
        """,
        (task_id,),
    ).fetchone()


def _serialize_task(row):
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "title": row["title"],
        "description": row["description"],
        "assignee_id": row["assignee_public_id"],
        "assignee_display_name": row["assignee_display_name"],
        "assignee_github_username": row["assignee_github_username"],
        "status": row["status"],
        "priority": row["priority"],
        "progress_percent": row["progress_percent"],
        "progress_is_estimate": bool(row["progress_is_estimate"]),
        "external_key": row["external_key"],
        "due_date": row["due_date"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _require_task_access(task, device):
    if task is None or task["project_id"] != device["project_id"]:
        raise HTTPException(status_code=404, detail="Task not found")
    if not (
        _is_project_head(device)
        or task["assignee_id"] == device["user_id"]
    ):
        raise HTTPException(status_code=404, detail="Task not found")


def _authenticate_device(authorization: Optional[str], scope: Optional[str]):
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")

    token_hash = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT devices.id AS device_id, devices.user_id, devices.project_id,
                   devices.scopes, devices.auth_mode, users.public_id AS actor_id,
                   users.github_login, users.github_username, users.email,
                   users.display_name, users.role AS user_role,
                   devices.label, projects.slug AS project_slug, projects.project_key,
                   projects.name AS project_name, projects.project_head_id,
                   project_memberships.role AS membership_role
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
    if row["auth_mode"] == "development" and not _development_auth_enabled():
        raise HTTPException(status_code=401, detail="Development credential disabled")

    try:
        scopes = json.loads(row["scopes"])
    except (TypeError, JSONDecodeError):
        scopes = []
    if scope is not None and scope not in scopes:
        raise HTTPException(
            status_code=403, detail="Credential lacks required permission"
        )
    return row


def _authenticate_api_key(authorization: Optional[str]):
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip() or not token.strip().startswith("trc_"):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    digest = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT api_keys.*, users.display_name, users.github_login
            FROM api_keys JOIN users ON users.id = api_keys.user_id
            WHERE api_keys.key_hash = ? AND api_keys.revoked_at IS NULL
        """, (digest,)).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now_iso(), row["id"]))
        conn.commit()
        return row
    finally:
        conn.close()


def _redact_secret_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    result = value
    for pattern in SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def _validate_new_event(raw: dict):
    if not isinstance(raw.get("type"), str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:\.[a-z0-9_-]+)+", raw["type"]) or len(raw["type"]) > 64:
        raise ValueError("type")
    for field, maximum in (("summary", 500), ("reason", 2000), ("task", 200), ("repo", 200), ("tool", 80), ("externalId", 200)):
        if raw.get(field) is not None and (not isinstance(raw[field], str) or len(raw[field]) > maximum):
            raise ValueError(field)
    if raw.get("confidence") is not None and (not isinstance(raw["confidence"], (int, float)) or not 0 <= raw["confidence"] <= 1):
        raise ValueError("confidence")
    payload = raw.get("payload", {})
    if not isinstance(payload, dict) or len(json.dumps(payload)) > 8192:
        raise ValueError("payload")
    if any(str(k).lower() in FORBIDDEN_PAYLOAD_KEYS for k in payload):
        raise ValueError("forbidden_payload_key")
    return payload


def _key_owner(authorization):
    try:
        return _authenticate_device(authorization, None)["user_id"]
    except HTTPException:
        raise


@app.post("/v1/api-keys", status_code=201)
@app.post("/v1/settings/api-keys", status_code=201)
def create_api_key(request: Request, authorization: Optional[str] = Header(default=None)):
    user_id = _key_owner(authorization)
    name = "API key"
    token = "trc_" + secrets.token_urlsafe(32)
    conn = get_connection()
    try:
        cur = conn.execute("INSERT INTO api_keys (user_id, key_hash, name, created_at) VALUES (?, ?, ?, ?)", (user_id, hashlib.sha256(token.encode()).hexdigest(), name, now_iso()))
        conn.commit()
        return {"id": cur.lastrowid, "name": name, "api_key": token, "created_at": now_iso()}
    finally: conn.close()


@app.get("/v1/api-keys")
@app.get("/v1/settings/api-keys")
def list_api_keys(authorization: Optional[str] = Header(default=None)):
    user_id = _key_owner(authorization)
    conn = get_connection()
    try:
        return [dict(row) for row in conn.execute("SELECT id, name, created_at, last_used_at, revoked_at FROM api_keys WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()]
    finally: conn.close()


@app.delete("/v1/api-keys/{key_id}")
@app.delete("/v1/settings/api-keys/{key_id}")
def revoke_api_key(key_id: int, authorization: Optional[str] = Header(default=None)):
    user_id = _key_owner(authorization)
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL", (now_iso(), key_id, user_id))
        conn.commit()
        if not cur.rowcount: raise HTTPException(status_code=404, detail="API key not found")
        return {"revoked": True}
    finally: conn.close()


def _credential_scopes(device):
    try:
        scopes = json.loads(device["scopes"])
    except (TypeError, JSONDecodeError):
        return []
    return [scope for scope in scopes if scope in {"events:read", "events:ingest"}]


def _check_persistent_rate_limit(
    conn, bucket_key: str, *, limit: int, window_seconds: int
):
    now = int(time.time())
    conn.execute(
        "DELETE FROM api_rate_limits WHERE window_started_at < ?",
        (now - 24 * 60 * 60,),
    )
    row = conn.execute(
        "SELECT window_started_at, request_count FROM api_rate_limits WHERE bucket_key = ?",
        (bucket_key,),
    ).fetchone()
    if row is None or now - row["window_started_at"] >= window_seconds:
        conn.execute(
            """
            INSERT INTO api_rate_limits (bucket_key, window_started_at, request_count)
            VALUES (?, ?, 1)
            ON CONFLICT(bucket_key) DO UPDATE SET
                window_started_at = excluded.window_started_at,
                request_count = 1
            """,
            (bucket_key, now),
        )
        return
    if row["request_count"] >= limit:
        retry_after = max(1, window_seconds - (now - row["window_started_at"]))
        raise HTTPException(
            status_code=429,
            detail="Pairing rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
    conn.execute(
        "UPDATE api_rate_limits SET request_count = request_count + 1 WHERE bucket_key = ?",
        (bucket_key,),
    )


def _pairing_client_bucket(request: Request) -> str:
    client_host = request.client.host if request.client else "unknown"
    return "pairing:claim:" + hashlib.sha256(client_host.encode("utf-8")).hexdigest()


@app.get("/v1/me/device")
def get_current_device(authorization: Optional[str] = Header(default=None)):
    device = _authenticate_device(authorization, "events:read")
    conn = get_connection()
    try:
        last_reported = conn.execute(
            """
            SELECT MAX(received_at) FROM activity_events
            WHERE device_id = ?
            """,
            (device["device_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "device_id": device["device_id"],
        "user_id": device["actor_id"],
        "display_name": device["display_name"],
        "email": device["email"],
        "github_username": device["github_username"],
        "role": device["membership_role"],
        "project_id": device["project_id"],
        "project_name": device["project_name"],
        "project_key": device["project_key"],
        "scopes": _credential_scopes(device),
        "label": device["label"],
        "last_reported_at": last_reported,
    }


@app.post("/v1/pairings", status_code=201)
def create_device_pairing(
    payload: PairingCreate,
    authorization: Optional[str] = Header(default=None),
):
    device = _authenticate_device(authorization, "events:ingest")
    if str(payload.project_id) != device["project_id"]:
        raise HTTPException(status_code=404, detail="Project not found")
    issuer_scopes = set(_credential_scopes(device))
    requested_scopes = set(payload.scopes)
    if len(requested_scopes) != len(payload.scopes) or not requested_scopes:
        raise HTTPException(status_code=422, detail="Invalid requested scopes")
    if not requested_scopes.issubset(issuer_scopes):
        raise HTTPException(status_code=403, detail="Cannot grant unowned scopes")

    pairing_code = secrets.token_urlsafe(24)
    pairing_id = str(uuid4())
    created_at = datetime.now(timezone.utc)
    expires_at = (created_at + timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_persistent_rate_limit(
            conn,
            f"pairing:create:{device['device_id']}",
            limit=5,
            window_seconds=60 * 60,
        )
        conn.execute(
            """
            INSERT INTO device_pairings
                (id, code_hash, user_id, project_id, scopes,
                 created_by_device_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pairing_id,
                hashlib.sha256(pairing_code.encode("utf-8")).hexdigest(),
                device["user_id"],
                device["project_id"],
                json.dumps(sorted(requested_scopes)),
                device["device_id"],
                created_at.isoformat(),
                expires_at,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "pairing_id": pairing_id,
        "pairing_code": pairing_code,
        "expires_at": expires_at,
        "scopes": sorted(requested_scopes),
    }


@app.post("/v1/pairings/claim")
def claim_device_pairing(payload: PairingClaim, request: Request):
    code_hash = hashlib.sha256(payload.code.encode("utf-8")).hexdigest()
    token = secrets.token_urlsafe(32)
    timestamp = now_iso()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_persistent_rate_limit(
            conn,
            _pairing_client_bucket(request),
            limit=10,
            window_seconds=60,
        )
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        pairing = conn.execute(
            """
            SELECT device_pairings.*, projects.project_key
            FROM device_pairings
            JOIN projects ON projects.id = device_pairings.project_id
            JOIN project_memberships
              ON project_memberships.project_id = device_pairings.project_id
             AND project_memberships.user_id = device_pairings.user_id
            JOIN devices AS issuer
              ON issuer.id = device_pairings.created_by_device_id
             AND issuer.revoked_at IS NULL
            WHERE device_pairings.code_hash = ?
              AND device_pairings.consumed_at IS NULL
              AND device_pairings.revoked_at IS NULL
              AND device_pairings.expires_at > ?
            """,
            (code_hash, timestamp),
        ).fetchone()
        if pairing is None:
            raise HTTPException(status_code=400, detail="Invalid or expired pairing code")
        cursor = conn.execute(
            """
            UPDATE device_pairings SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL
            """,
            (timestamp, pairing["id"]),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=400, detail="Invalid or expired pairing code")
        device_cursor = conn.execute(
            """
            INSERT INTO devices
                (user_id, token_hash, label, created_at, project_id, scopes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                pairing["user_id"],
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                payload.device_label,
                timestamp,
                pairing["project_id"],
                pairing["scopes"],
            ),
        )
        conn.execute(
            "UPDATE device_pairings SET claimed_device_id = ? WHERE id = ?",
            (device_cursor.lastrowid, pairing["id"]),
        )
        scopes = json.loads(pairing["scopes"])
        conn.commit()
        return {
            "access_token": token,
            "token_type": "bearer",
            "device_id": device_cursor.lastrowid,
            "project_id": pairing["project_id"],
            "project_key": pairing["project_key"],
            "user_id": str(
                conn.execute(
                    "SELECT public_id FROM users WHERE id = ?",
                    (pairing["user_id"],),
                ).fetchone()["public_id"]
            ),
            "scopes": scopes,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/v1/pairings/{pairing_id}")
def get_pairing_status(
    pairing_id: str, authorization: Optional[str] = Header(default=None)
):
    device = _authenticate_device(authorization, "events:ingest")
    try:
        pairing_id = str(UUID(pairing_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Pairing not found") from exc
    conn = get_connection()
    try:
        pairing = conn.execute(
            """
            SELECT device_pairings.*, devices.label AS claimed_device_label,
                   devices.revoked_at AS claimed_device_revoked_at
            FROM device_pairings
            LEFT JOIN devices ON devices.id = device_pairings.claimed_device_id
            WHERE device_pairings.id = ?
              AND device_pairings.created_by_device_id = ?
              AND device_pairings.project_id = ?
            """,
            (pairing_id, device["device_id"], device["project_id"]),
        ).fetchone()
    finally:
        conn.close()
    if pairing is None:
        raise HTTPException(status_code=404, detail="Pairing not found")
    if pairing["revoked_at"]:
        status = "revoked"
    elif pairing["consumed_at"]:
        status = (
            "revoked"
            if pairing["claimed_device_revoked_at"]
            else "claimed"
        )
    elif datetime.fromisoformat(pairing["expires_at"].replace("Z", "+00:00")) <= datetime.now(
        timezone.utc
    ):
        status = "expired"
    else:
        status = "pending"
    return {
        "pairing_id": pairing_id,
        "status": status,
        "expires_at": pairing["expires_at"],
        "device_id": pairing["claimed_device_id"],
        "device_label": pairing["claimed_device_label"],
    }


@app.delete("/v1/me/device", status_code=204)
def revoke_current_device(authorization: Optional[str] = Header(default=None)):
    device = _authenticate_device(authorization, None)
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now_iso(), device["device_id"]),
        )
        conn.commit()
    finally:
        conn.close()


def _device_is_active(
    device_id: int, user_id: int, project_id: str, scope: str
) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT devices.scopes, devices.auth_mode FROM devices
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
    if row["auth_mode"] == "development" and not _development_auth_enabled():
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
        "git.pull_request.updated": "Updated a pull request",
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
               users.display_name AS actor_display_name,
               users.github_username AS actor_github_username,
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
    task = None
    if event.task_id is not None:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND project_id = ?",
            (str(event.task_id), device["project_id"]),
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not (
            _is_project_head(device)
            or task["assignee_id"] == device["user_id"]
        ):
            raise HTTPException(
                status_code=403, detail="Not authorized to report on this task"
            )
    existing = conn.execute(
        "SELECT project_id FROM activity_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if existing is not None:
        if existing["project_id"] != device["project_id"]:
            raise HTTPException(status_code=409, detail="Event ID already exists")
        return False

    data = event.metadata
    device_id = device["device_id"] if "device_id" in device.keys() else None
    conn.execute(
        """
        INSERT INTO activity_events
            (event_id, schema_version, type, ts, received_at, user_id, repo, branch,
             source, data, project_id, task_id, device_id)
        VALUES (?, 2, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
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
            device_id,
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
    if task is not None:
        _apply_task_event(conn, task, event, device, event_id, received_at)
    return True


def _apply_task_event(conn, task, event, device, event_id: str, timestamp: str):
    progress_percent = None
    progress_is_estimate = False
    update_fields = {}

    if event.event_type == "task.started":
        update_fields["status"] = "in_progress"
    elif event.event_type == "task.progress":
        update_fields["status"] = "in_progress"
        progress_percent = event.metadata["progress"]
        progress_is_estimate = True
        update_fields["progress_percent"] = progress_percent
        update_fields["progress_is_estimate"] = 1
    elif event.event_type == "task.blocked":
        update_fields["status"] = "blocked"
    elif event.event_type == "task.completed":
        outcome = event.metadata["outcome"]
        update_fields["status"] = {
            "done": "done",
            "partial": "in_progress",
            "abandoned": "todo",
        }[outcome]
        if outcome == "done":
            update_fields["progress_percent"] = 100
            update_fields["progress_is_estimate"] = 0
            progress_percent = 100

    if update_fields:
        assignments = ", ".join(f"{key} = ?" for key in update_fields)
        conn.execute(
            f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",
            (*update_fields.values(), timestamp, task["id"]),
        )
    conn.execute(
        """
        INSERT INTO task_updates
            (id, task_id, actor_id, event_id, update_type, note,
             progress_percent, progress_is_estimate, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            task["id"],
            device["user_id"],
            event_id,
            event.event_type,
            event.metadata.get("note"),
            progress_percent,
            int(progress_is_estimate),
            timestamp,
        ),
    )


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
             repo, branch, source, tool, data, project_id, device_id)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            device["device_id"],
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


TASK_REFERENCE_PATTERN = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_-]{0,63}-[1-9][0-9]{0,11})\b"
)
GITHUB_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
)
GITHUB_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")


def _github_task_reference(conn, project_id: str, *values):
    references = {
        match.group(1).upper()
        for value in values
        if isinstance(value, str)
        for match in TASK_REFERENCE_PATTERN.finditer(value)
    }
    if len(references) != 1:
        return None, None
    reference = references.pop()
    task = conn.execute(
        """
        SELECT id, external_key FROM tasks
        WHERE project_id = ? AND external_key = ? COLLATE NOCASE
        """,
        (project_id, reference),
    ).fetchone()
    if task is None:
        return None, None
    return task["id"], task["external_key"]


def _github_timestamp(value):
    if not isinstance(value, str):
        return now_iso()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return now_iso()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return now_iso()
    return parsed.astimezone(timezone.utc).isoformat()


def _github_event_id(delivery_id: str, event_type: str, subject: str):
    return str(
        uuid5(
            NAMESPACE_URL,
            f"trac:github-delivery:{delivery_id}:{event_type}:{subject}",
        )
    )


def _github_build_result(conclusion):
    if conclusion == "success":
        return "passed"
    if conclusion in {
        "failure",
        "timed_out",
        "action_required",
        "startup_failure",
    }:
        return "failed"
    if conclusion in {
        "cancelled",
        "canceled",
        "neutral",
        "skipped",
        "stale",
    }:
        return "cancelled"
    return None


def _github_duration_ms(started_at, completed_at):
    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if (
        start.tzinfo is None
        or start.utcoffset() is None
        or end.tzinfo is None
        or end.utcoffset() is None
    ):
        return None
    duration = (end - start).total_seconds()
    if duration < 0:
        return None
    return int(duration * 1000)


def _safe_ci_label(value):
    if (
        isinstance(value, str)
        and len(value) <= 120
        and re.fullmatch(r"[A-Za-z0-9_. /-]+", value)
    ):
        return value
    return None


def _normalize_github_payload(
    conn,
    *,
    event_name: str,
    delivery_id: str,
    payload: Dict[str, Any],
    project_id: str,
):
    repository = payload.get("repository")
    repository_name = (
        repository.get("full_name") if isinstance(repository, dict) else None
    )
    if not isinstance(repository_name, str) or not GITHUB_REPOSITORY_PATTERN.fullmatch(
        repository_name
    ):
        raise HTTPException(status_code=422, detail="Invalid repository in payload")

    events = []

    def add_event(
        event_type, source, occurred_at, task_id, task_reference, metadata, subject
    ):
        raw_event = {
            "event_id": _github_event_id(delivery_id, event_type, subject),
            "schema_version": 2,
            "event_type": event_type,
            "task_id": task_id,
            "source": source,
            "occurred_at": _github_timestamp(occurred_at),
            "metadata": {
                **metadata,
                "repository": repository_name,
                **({"task_reference": task_reference} if task_reference else {}),
            },
        }
        try:
            event = CanonicalEvent.model_validate(raw_event)
            _reject_sensitive_values(event.metadata)
        except (ValidationError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="Invalid normalized GitHub event"
            ) from exc
        events.append(event)

    if event_name == "push":
        ref = payload.get("ref")
        branch = (
            ref[len("refs/heads/") :]
            if isinstance(ref, str) and ref.startswith("refs/heads/")
            else None
        )
        head = payload.get("head_commit")
        commits = payload.get("commits")
        commit = head if isinstance(head, dict) else None
        if commit is None and isinstance(commits, list) and commits:
            commit = commits[-1] if isinstance(commits[-1], dict) else None
        if (
            commit is not None
            and isinstance(commit.get("id"), str)
            and re.fullmatch(r"[0-9a-fA-F]{7,64}", commit["id"])
            and isinstance(branch, str)
            and GITHUB_REF_PATTERN.fullmatch(branch)
        ):
            task_id, task_reference = _github_task_reference(
                conn, project_id, branch
            )
            add_event(
                "git.commit",
                "github",
                commit.get("timestamp") or payload.get("pushed_at"),
                task_id,
                task_reference,
                {"commit_sha": commit["id"], "branch": branch},
                commit["id"].lower(),
            )
    elif event_name == "pull_request":
        action = payload.get("action")
        pull_request = payload.get("pull_request")
        if isinstance(pull_request, dict):
            number = pull_request.get("number")
            base = pull_request.get("base")
            head = pull_request.get("head")
            base_ref = base.get("ref") if isinstance(base, dict) else None
            head_ref = head.get("ref") if isinstance(head, dict) else None
            if (
                isinstance(number, int)
                and number > 0
                and isinstance(base_ref, str)
                and GITHUB_REF_PATTERN.fullmatch(base_ref)
                and isinstance(head_ref, str)
                and GITHUB_REF_PATTERN.fullmatch(head_ref)
            ):
                if action == "opened":
                    event_type = "git.pull_request.opened"
                elif action == "synchronize":
                    event_type = "git.pull_request.updated"
                elif action == "closed" and pull_request.get("merged") is True:
                    event_type = "git.pull_request.merged"
                else:
                    event_type = None
                if event_type:
                    task_id, task_reference = _github_task_reference(
                        conn,
                        project_id,
                        pull_request.get("title"),
                        head_ref,
                    )
                    add_event(
                        event_type,
                        "github",
                        pull_request.get("updated_at")
                        or pull_request.get("created_at"),
                        task_id,
                        task_reference,
                        {
                            "pull_request_number": number,
                            "base_branch": base_ref,
                            "head_branch": head_ref,
                        },
                        f"pr:{number}:{action}",
                    )
    elif event_name == "workflow_run":
        workflow_run = payload.get("workflow_run")
        if isinstance(workflow_run, dict) and payload.get("action") == "completed":
            result = _github_build_result(workflow_run.get("conclusion"))
            branch = workflow_run.get("head_branch")
            if result and (
                branch is None
                or (
                    isinstance(branch, str)
                    and GITHUB_REF_PATTERN.fullmatch(branch)
                )
            ):
                task_id, task_reference = _github_task_reference(
                    conn, project_id, branch
                )
                add_event(
                    "build.completed",
                    "ci",
                    workflow_run.get("updated_at")
                    or workflow_run.get("completed_at"),
                    task_id,
                    task_reference,
                    {
                        "result": result,
                        "duration_ms": _github_duration_ms(
                            workflow_run.get("run_started_at"),
                            workflow_run.get("updated_at")
                            or workflow_run.get("completed_at"),
                        ),
                        "workflow_name": _safe_ci_label(workflow_run.get("name")),
                        "workflow_id": workflow_run.get("workflow_id"),
                        "run_id": workflow_run.get("id"),
                        "branch": branch,
                    },
                    f"workflow:{workflow_run.get('id')}",
                )
    elif event_name == "check_run":
        check_run = payload.get("check_run")
        if isinstance(check_run, dict) and payload.get("action") == "completed":
            result = _github_build_result(check_run.get("conclusion"))
            check_suite = check_run.get("check_suite")
            branch = (
                check_suite.get("head_branch")
                if isinstance(check_suite, dict)
                else None
            )
            if branch is None:
                branch = check_run.get("head_branch")
            if result and (
                branch is None
                or (
                    isinstance(branch, str)
                    and GITHUB_REF_PATTERN.fullmatch(branch)
                )
            ):
                task_id, task_reference = _github_task_reference(
                    conn, project_id, branch
                )
                add_event(
                    "build.completed",
                    "ci",
                    check_run.get("completed_at"),
                    task_id,
                    task_reference,
                    {
                        "result": result,
                        "duration_ms": _github_duration_ms(
                            check_run.get("started_at"),
                            check_run.get("completed_at"),
                        ),
                        "check_name": _safe_ci_label(check_run.get("name")),
                        "check_run_id": check_run.get("id"),
                        "branch": branch,
                    },
                    f"check:{check_run.get('id')}",
                )
    return events


@app.post("/v1/integrations/github/webhook", status_code=202)
async def receive_github_webhook(request: Request):
    secret = os.environ.get("TRAC_GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(
            status_code=503, detail="GitHub webhook integration is not configured"
        )
    body = await _read_limited_body(request)
    signature = request.headers.get("x-hub-signature-256", "")
    expected_signature = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature) or not hmac.compare_digest(
        signature, expected_signature
    ):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")

    delivery_id = request.headers.get("x-github-delivery", "")
    event_name = request.headers.get("x-github-event", "")
    try:
        delivery_id = str(UUID(delivery_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid GitHub delivery ID"
        ) from exc
    if not event_name or len(event_name) > 80:
        raise HTTPException(status_code=400, detail="Invalid GitHub event header")
    try:
        payload = json.loads(body)
    except (JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=422, detail="Request body must be valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Webhook body must be an object")

    supported_events = {"push", "pull_request", "workflow_run", "check_run"}
    if event_name not in supported_events:
        return {"accepted": 0, "ignored": True}

    repository = payload.get("repository")
    repository_name = (
        repository.get("full_name") if isinstance(repository, dict) else None
    )
    if not isinstance(repository_name, str) or not GITHUB_REPOSITORY_PATTERN.fullmatch(
        repository_name
    ):
        raise HTTPException(status_code=422, detail="Invalid repository in payload")

    conn = get_connection()
    received_at = now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT 1 FROM github_webhook_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        if duplicate is not None:
            conn.commit()
            return {"accepted": 0, "duplicate": True}

        mapping = conn.execute(
            """
            SELECT github_repository_mappings.project_id,
                   github_repository_mappings.actor_user_id,
                   users.github_login, users.display_name,
                   projects.project_head_id
            FROM github_repository_mappings
            JOIN users ON users.id = github_repository_mappings.actor_user_id
            JOIN projects ON projects.id = github_repository_mappings.project_id
            WHERE github_repository_mappings.repository = ?
            """,
            (repository_name,),
        ).fetchone()
        if mapping is None:
            raise HTTPException(status_code=404, detail="Repository is not mapped")

        events = _normalize_github_payload(
            conn,
            event_name=event_name,
            delivery_id=delivery_id,
            payload=payload,
            project_id=mapping["project_id"],
        )
        conn.execute(
            """
            INSERT INTO github_webhook_deliveries
                (delivery_id, event_name, repository, project_id, received_at,
                 normalized_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                event_name,
                repository_name,
                mapping["project_id"],
                received_at,
                len(events),
            ),
        )
        integration_actor = {
            "user_id": mapping["actor_user_id"],
            "project_id": mapping["project_id"],
            "actor_id": None,
            "github_login": mapping["github_login"],
            "display_name": mapping["display_name"],
            "project_head_id": mapping["actor_user_id"],
            "membership_role": "owner",
        }
        accepted = 0
        for event in events:
            if _ingest_canonical(conn, event, integration_actor, received_at):
                accepted += 1
        conn.commit()
        return {"accepted": accepted, "duplicate": False}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/v1/events", status_code=202)
async def create_activity_events(request: Request):
    authorization = request.headers.get("authorization")
    is_api_key = bool(authorization and authorization.lower().startswith("bearer trc_"))
    api_key = _authenticate_api_key(authorization) if is_api_key else None
    device = None if is_api_key else _authenticate_device(authorization, "events:ingest")
    retry_after = _check_rate_limit(device["device_id"]) if device else None
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

    if is_api_key and any(isinstance(item, dict) and (NEW_EVENT_KEYS & set(item)) for item in raw_events):
        inserted = 0
        deduplicated = 0
        rejected = []
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for index, raw in enumerate(raw_events):
                try:
                    if not isinstance(raw, dict):
                        raise ValueError("event")
                    payload_data = _validate_new_event(raw)
                    occurred = raw.get("occurredAt") or now_iso()
                    datetime.fromisoformat(occurred.replace("Z", "+00:00"))
                    summary = _redact_secret_text(raw.get("summary", "")) or ""
                    reason = _redact_secret_text(raw.get("reason"))
                    cur = conn.execute("""
                        INSERT OR IGNORE INTO events
                        (type, source, summary, repo, actor, actor_user_id, actor_hint, task_key, reason,
                         confidence, occurred_at, external_id, tool, payload, api_key_id, timestamp, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
                    """, (raw["type"], raw.get("tool") or "api", summary, raw.get("repo"),
                          api_key["display_name"], api_key["user_id"], raw.get("engineer"), raw.get("task"),
                          reason, raw.get("confidence"), occurred, raw.get("externalId"), raw.get("tool"),
                          json.dumps(payload_data), api_key["id"], now_iso()))
                    if cur.rowcount:
                        inserted += 1
                    else:
                        deduplicated += 1
                except (KeyError, TypeError, ValueError, OverflowError):
                    rejected.append({"index": index, "reason": "invalid_event"})
            conn.commit()
        finally:
            conn.close()
        return Response(content=json.dumps({"inserted": inserted, "deduplicated": deduplicated, "rejected": rejected}), media_type="application/json", status_code=201)

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
            visibility, visibility_params = _event_visibility_clause(device)
            cursor = conn.execute(
                """
                SELECT activity_event_stream.sequence
                FROM activity_event_stream
                JOIN activity_events
                  ON activity_events.event_id = activity_event_stream.event_id
                WHERE activity_event_stream.event_id = ?
                  AND activity_event_stream.project_id = ?
                """
                + visibility,
                (last_event_id, device["project_id"], *visibility_params),
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
                           activity_events.user_id,
                           assigned_task.assignee_id AS task_assignee_id,
                           users.public_id AS actor_id,
                           users.display_name AS actor_display_name,
                           activity_events.device_id, activity_events.source,
                           activity_events.tool,
                           activity_events.ts AS occurred_at,
                           activity_events.received_at,
                           activity_events.data AS metadata
                    FROM activity_event_stream
                    JOIN activity_events
                      ON activity_events.event_id = activity_event_stream.event_id
                    JOIN users ON users.id = activity_events.user_id
                    LEFT JOIN tasks AS assigned_task
                      ON assigned_task.id = activity_events.task_id
                    WHERE activity_event_stream.project_id = ?
                      AND activity_event_stream.sequence > ?
                    ORDER BY activity_event_stream.sequence LIMIT 100
                    """,
                    (device["project_id"], sequence),
                ).fetchall()
            finally:
                conn.close()
            if rows:
                emitted = False
                for row in rows:
                    sequence = row["sequence"]
                    result = dict(row)
                    result.pop("sequence", None)
                    if not _is_project_head(device) and not (
                        result["user_id"] == device["user_id"]
                        or result["task_assignee_id"] == device["user_id"]
                    ):
                        continue
                    result.pop("user_id", None)
                    result.pop("task_assignee_id", None)
                    result.pop("device_id", None)
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
                    emitted = True
                if emitted:
                    heartbeat_at = time.monotonic()
                else:
                    await asyncio.sleep(STREAM_POLL_SECONDS)
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


@app.get("/v1/stream")
async def stream_personal_events(request: Request, authorization: Optional[str] = Header(default=None)):
    key = _authenticate_api_key(authorization)
    async def stream():
        last_id = 0
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            conn = get_connection()
            try:
                rows = conn.execute("SELECT events.*, users.display_name AS actor_name, users.github_username AS actor_avatar FROM events JOIN users ON users.id = events.actor_user_id WHERE events.api_key_id = ? AND events.id > ? ORDER BY events.id LIMIT 100", (key["id"], last_id)).fetchall()
            finally: conn.close()
            for row in rows:
                last_id = row["id"]
                yield f"id: {last_id}\nevent: event\ndata: {json.dumps(dict(row), separators=(',', ':'))}\n\n"
            if not rows: await asyncio.sleep(STREAM_POLL_SECONDS)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/v1/events")
def list_activity_events(
    response: Response,
    authorization: Optional[str] = Header(default=None),
    limit: int = 100,
    actor: Optional[str] = None,
    type: Optional[str] = None,
    repo: Optional[str] = None,
    task: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    search: Optional[str] = None,
    cursor: Optional[int] = None,
):
    if authorization and authorization.lower().startswith("bearer trc_"):
        key = _authenticate_api_key(authorization)
        if not 1 <= limit <= 200:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
        clauses = ["api_key_id = ?"]
        params: list = [key["id"]]
        for column, value in (("type", type), ("repo", repo), ("task_key", task), ("actor", actor)):
            if value:
                clauses.append(f"{column} = ?"); params.append(value)
        if since: clauses.append("occurred_at >= ?"); params.append(since)
        if until: clauses.append("occurred_at <= ?"); params.append(until)
        if search: clauses.append("(summary LIKE ? OR reason LIKE ?)"); params.extend([f"%{search}%", f"%{search}%"])
        if cursor: clauses.append("events.id < ?"); params.append(cursor)
        conn = get_connection()
        try:
            rows = conn.execute(f"SELECT events.*, users.display_name AS actor_name, users.github_username AS actor_avatar FROM events JOIN users ON users.id = events.actor_user_id WHERE {' AND '.join(clauses)} ORDER BY events.id DESC LIMIT ?", (*params, limit)).fetchall()
        finally: conn.close()
        return [dict(row) for row in rows]
    device = _authenticate_device(authorization, "events:read")
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        visibility, visibility_params = _event_visibility_clause(device)
        rows = conn.execute(
            """
            SELECT event_id FROM activity_events
            WHERE project_id = ? AND source != 'compat-api'
            """
            + visibility
            + """
            ORDER BY received_at DESC LIMIT ?
            """,
            (device["project_id"], *visibility_params, limit),
        ).fetchall()
        events = [
            _canonical_response(conn, row["event_id"], device["project_id"])
            for row in rows
        ]
        cursor = conn.execute(
            """
            SELECT activity_event_stream.event_id
            FROM activity_event_stream
            JOIN activity_events
              ON activity_events.event_id = activity_event_stream.event_id
            WHERE activity_event_stream.project_id = ?
            """
            + visibility
            + """
            ORDER BY activity_event_stream.sequence DESC LIMIT 1
            """,
            (device["project_id"], *visibility_params),
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
        visibility, visibility_params = _event_visibility_clause(device)
        visible = conn.execute(
            """
            SELECT 1 FROM activity_events
            WHERE event_id = ? AND project_id = ?
            """
            + visibility,
            (event_id, device["project_id"], *visibility_params),
        ).fetchone()
        event = (
            _canonical_response(conn, event_id, device["project_id"])
            if visible
            else None
        )
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
             source, tool, data, project_id, device_id)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'compat-api', ?, ?, ?, ?)
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
            device["device_id"],
        ),
    )
    conn.execute(
        """
        INSERT INTO activity_event_stream (event_id, project_id, created_at)
        VALUES (?, ?, ?)
        """,
        (event_id, device["project_id"], timestamp),
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
                    WHERE execution_id IS NOT NULL DO NOTHING
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
        visibility = ""
        visibility_params = ()
        if not _is_project_head(device):
            visibility = """
              AND (
                  events.actor_user_id = ?
                  OR EXISTS (
                      SELECT 1 FROM activity_events
                      JOIN tasks ON tasks.id = activity_events.task_id
                      WHERE activity_events.event_id = events.activity_event_id
                        AND tasks.assignee_id = ?
                  )
              )
            """
            visibility_params = (device["user_id"], device["user_id"])
        if source:
            rows = conn.execute(
                """
                SELECT * FROM events WHERE project_id = ? AND source = ?
                """
                + visibility
                + """
                ORDER BY timestamp DESC LIMIT ?
                """,
                (device["project_id"], source, *visibility_params, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM events WHERE project_id = ?
                """
                + visibility
                + """
                ORDER BY timestamp DESC LIMIT ?
                """,
                (device["project_id"], *visibility_params, limit),
            ).fetchall()
        stream_visibility, stream_visibility_params = _event_visibility_clause(device)
        cursor = conn.execute(
            """
            SELECT activity_event_stream.event_id
            FROM activity_event_stream
            JOIN activity_events
              ON activity_events.event_id = activity_event_stream.event_id
            WHERE activity_event_stream.project_id = ?
            """
            + stream_visibility
            + """
            ORDER BY activity_event_stream.sequence DESC LIMIT 1
            """,
            (device["project_id"], *stream_visibility_params),
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
        visibility = ""
        visibility_params = ()
        if not _is_project_head(device):
            visibility = """
              AND (
                  events.actor_user_id = ?
                  OR EXISTS (
                      SELECT 1 FROM activity_events
                      JOIN tasks ON tasks.id = activity_events.task_id
                      WHERE activity_events.event_id = events.activity_event_id
                        AND tasks.assignee_id = ?
                  )
              )
            """
            visibility_params = (device["user_id"], device["user_id"])
        row = conn.execute(
            "SELECT * FROM events WHERE id = ? AND project_id = ?" + visibility,
            (event_id, device["project_id"], *visibility_params),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_response(row)


@app.post("/v1/dev/login")
def development_login(payload: DevLoginRequest):
    if not _development_auth_enabled():
        raise HTTPException(status_code=404, detail="Development login is disabled")

    conn = get_connection()
    try:
        memberships = conn.execute(
            """
            SELECT users.id AS user_id, users.public_id, users.display_name,
                   users.github_username, projects.id AS project_id,
                   project_memberships.role
            FROM users
            JOIN project_memberships
              ON project_memberships.user_id = users.id
            JOIN projects ON projects.id = project_memberships.project_id
            WHERE lower(COALESCE(users.github_username, users.github_login)) = lower(?)
              AND projects.id = ?
            """,
            (payload.github_username, str(payload.project_id)),
        ).fetchall()
        if len(memberships) != 1:
            raise HTTPException(status_code=401, detail="Unknown user or project")
        membership = memberships[0]

        token = secrets.token_urlsafe(32)
        scopes = ["events:read"]
        if membership["role"] in {"owner", "member"}:
            scopes.append("events:ingest")
        conn.execute(
            """
            INSERT INTO devices
                (user_id, token_hash, label, created_at, project_id, scopes, auth_mode)
            VALUES (?, ?, 'Development login', ?, ?, ?, 'development')
            """,
            (
                membership["user_id"],
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                now_iso(),
                membership["project_id"],
                json.dumps(scopes),
            ),
        )
        conn.commit()
        return {
            "access_token": token,
            "token_type": "bearer",
            "user_id": membership["public_id"],
            "project_id": membership["project_id"],
            "role": membership["role"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _project_identifier(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def _project_response(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "project_key": row["project_key"],
        "project_head_id": row["head_public_id"],
        "project_head_name": row["head_display_name"],
        "member_count": row["member_count"],
        "members": json.loads(row["members_json"] or "[]"),
        "created_at": row["created_at"],
        "role": row["membership_role"],
    }


def _project_detail_row(conn, project_id: str, user_id: int):
    return conn.execute(
        """
        SELECT projects.*, users.public_id AS head_public_id,
               users.display_name AS head_display_name,
               project_memberships.role AS membership_role,
               (SELECT COUNT(*) FROM project_memberships AS members
                WHERE members.project_id = projects.id) AS member_count,
               (SELECT json_group_array(json_object(
                    'id', member_user.public_id,
                    'display_name', member_user.display_name,
                    'role', member_membership.role))
                FROM project_memberships AS member_membership
                JOIN users AS member_user ON member_user.id = member_membership.user_id
                WHERE member_membership.project_id = projects.id) AS members_json
        FROM projects
        JOIN project_memberships
          ON project_memberships.project_id = projects.id
         AND project_memberships.user_id = ?
        LEFT JOIN users ON users.id = projects.project_head_id
        WHERE projects.id = ?
        """,
        (user_id, project_id),
    ).fetchone()


@app.get("/v1/projects")
def list_projects(authorization: Optional[str] = Header(default=None)):
    device = _authenticate_device(authorization, "events:read")
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT projects.*, users.public_id AS head_public_id,
                   users.display_name AS head_display_name,
                   project_memberships.role AS membership_role,
                   (SELECT COUNT(*) FROM project_memberships AS members
                    WHERE members.project_id = projects.id) AS member_count,
                   (SELECT json_group_array(json_object(
                        'id', member_user.public_id,
                        'display_name', member_user.display_name,
                        'role', member_membership.role))
                    FROM project_memberships AS member_membership
                    JOIN users AS member_user ON member_user.id = member_membership.user_id
                    WHERE member_membership.project_id = projects.id) AS members_json
            FROM project_memberships
            JOIN projects ON projects.id = project_memberships.project_id
            LEFT JOIN users ON users.id = projects.project_head_id
            WHERE project_memberships.user_id = ?
            ORDER BY projects.name COLLATE NOCASE
            """,
            (device["user_id"],),
        ).fetchall()
        return [_project_response(row) for row in rows]
    finally:
        conn.close()


@app.post("/v1/projects", status_code=201)
def create_project(
    payload: ProjectCreate, authorization: Optional[str] = Header(default=None)
):
    device = _authenticate_device(authorization, "events:ingest")
    project_id = str(uuid4())
    token = secrets.token_urlsafe(32)
    timestamp = now_iso()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO projects
                (id, slug, name, description, project_key, project_head_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                payload.project_key,
                payload.name,
                payload.description,
                payload.project_key,
                device["user_id"],
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO project_memberships (project_id, user_id, role, created_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (project_id, device["user_id"], timestamp),
        )
        conn.execute(
            """
            INSERT INTO devices
                (user_id, token_hash, label, created_at, project_id, scopes)
            VALUES (?, ?, 'Project head API credential', ?, ?,
                    '["events:ingest","events:read"]')
            """,
            (
                device["user_id"],
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                timestamp,
                project_id,
            ),
        )
        conn.commit()
        row = _project_detail_row(conn, project_id, device["user_id"])
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(
            status_code=409, detail="Project key already exists"
        ) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = _project_response(row)
    result["device_token"] = token
    return result


@app.get("/v1/projects/{project_id}")
def get_project(project_id: str, authorization: Optional[str] = Header(default=None)):
    device = _authenticate_device(authorization, "events:read")
    project_id = _project_identifier(project_id)
    _require_project_device(device, project_id)
    conn = get_connection()
    try:
        row = _project_detail_row(conn, project_id, device["user_id"])
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_response(row)


@app.get("/v1/projects/{project_id}/devices")
def list_project_devices(
    project_id: str, authorization: Optional[str] = Header(default=None)
):
    device = _authenticate_device(authorization, "events:read")
    project_id = _project_identifier(project_id)
    _require_project_device(device, project_id)
    if not _is_project_head(device):
        raise HTTPException(
            status_code=403, detail="Only project heads can manage project devices"
        )
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT devices.id AS device_id, devices.label, devices.scopes,
                   devices.auth_mode, devices.created_at, devices.revoked_at,
                   users.public_id AS user_id, users.display_name,
                   projects.name AS project_name,
                   (SELECT MAX(activity_events.received_at)
                    FROM activity_events
                    WHERE activity_events.device_id = devices.id) AS last_reported_at
            FROM devices
            JOIN users ON users.id = devices.user_id
            JOIN projects ON projects.id = devices.project_id
            WHERE devices.project_id = ?
            ORDER BY devices.created_at DESC, devices.id DESC
            """,
            (project_id,),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        item = dict(row)
        item["scopes"] = json.loads(item["scopes"] or "[]")
        item["status"] = "revoked" if item["revoked_at"] else "authorized"
        result.append(item)
    return result


@app.delete("/v1/projects/{project_id}/devices/{target_device_id}", status_code=204)
def revoke_project_device(
    project_id: str,
    target_device_id: int,
    authorization: Optional[str] = Header(default=None),
):
    device = _authenticate_device(authorization, "events:ingest")
    project_id = _project_identifier(project_id)
    _require_project_device(device, project_id)
    if not _is_project_head(device):
        raise HTTPException(
            status_code=403, detail="Only project heads can revoke project devices"
        )
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE devices SET revoked_at = ?
            WHERE id = ? AND project_id = ? AND revoked_at IS NULL
            """,
            (now_iso(), target_device_id, project_id),
        )
        conn.commit()
    finally:
        conn.close()
    if cursor.rowcount != 1:
        raise HTTPException(status_code=404, detail="Device not found")
    return Response(status_code=204)


def _task_list_rows(conn, project_id: str, where: str = "", params=()):
    return conn.execute(
        """
        SELECT tasks.*, assignee.public_id AS assignee_public_id,
               assignee.display_name AS assignee_display_name,
               assignee.github_username AS assignee_github_username
        FROM tasks
        LEFT JOIN users AS assignee ON assignee.id = tasks.assignee_id
        WHERE tasks.project_id = ?
        """
        + where
        + " ORDER BY tasks.updated_at DESC, tasks.created_at DESC",
        (project_id, *params),
    ).fetchall()


def _task_updates(conn, task_id: str):
    rows = conn.execute(
        """
        SELECT task_updates.*, users.public_id AS actor_public_id,
               users.display_name AS actor_display_name,
               users.github_username AS actor_github_username
        FROM task_updates
        JOIN users ON users.id = task_updates.actor_id
        WHERE task_updates.task_id = ?
        ORDER BY task_updates.timestamp DESC, task_updates.id DESC
        """,
        (task_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "task_id": row["task_id"],
            "actor_id": row["actor_public_id"],
            "actor_display_name": row["actor_display_name"],
            "actor_github_username": row["actor_github_username"],
            "event_id": row["event_id"],
            "update_type": row["update_type"],
            "note": row["note"],
            "progress_percent": row["progress_percent"],
            "progress_is_estimate": bool(row["progress_is_estimate"]),
            "timestamp": row["timestamp"],
        }
        for row in rows
    ]


@app.get("/v1/projects/{project_id}/tasks")
def list_project_tasks(
    project_id: str, authorization: Optional[str] = Header(default=None)
):
    device = _authenticate_device(authorization, "events:read")
    project_id = _project_identifier(project_id)
    _require_project_device(device, project_id)
    where, params = (
        ("", ())
        if _is_project_head(device)
        else (" AND tasks.assignee_id = ?", (device["user_id"],))
    )
    conn = get_connection()
    try:
        rows = _task_list_rows(conn, project_id, where, params)
        return [_serialize_task(row) for row in rows]
    finally:
        conn.close()


@app.post("/v1/projects/{project_id}/tasks", status_code=201)
def create_project_task(
    project_id: str,
    payload: TaskCreate,
    authorization: Optional[str] = Header(default=None),
):
    device = _authenticate_device(authorization, "events:ingest")
    project_id = _project_identifier(project_id)
    _require_project_device(device, project_id)
    if not _is_project_head(device):
        raise HTTPException(
            status_code=403, detail="Only project heads can create tasks"
        )

    task_id = str(uuid4())
    timestamp = now_iso()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        assignee_id = None
        if payload.assignee_id is not None:
            assignee = conn.execute(
                """
                SELECT users.id FROM users
                JOIN project_memberships
                  ON project_memberships.user_id = users.id
                 AND project_memberships.project_id = ?
                WHERE users.public_id = ?
                  AND project_memberships.role IN ('owner', 'member')
                """,
                (project_id, str(payload.assignee_id)),
            ).fetchone()
            if assignee is None:
                raise HTTPException(
                    status_code=422,
                    detail="Assignee must be an owner or member of this project",
                )
            assignee_id = assignee["id"]
        if payload.external_key and conn.execute(
            """
            SELECT 1 FROM tasks
            WHERE project_id = ? AND external_key = ? COLLATE NOCASE
            """,
            (project_id, payload.external_key),
        ).fetchone():
            raise HTTPException(status_code=409, detail="Task key already exists")
        conn.execute(
            """
            INSERT INTO tasks
                (
                    id, project_id, title, description, external_key, assignee_id,
                    status, priority,
                    progress_percent, progress_is_estimate, due_date, created_at,
                    updated_at
                )
            VALUES (?, ?, ?, ?, ?, ?, 'todo', ?, 0, 0, ?, ?, ?)
            """,
            (
                task_id,
                project_id,
                payload.title,
                payload.description,
                payload.external_key,
                assignee_id,
                payload.priority,
                payload.due_date.isoformat() if payload.due_date else None,
                timestamp,
                timestamp,
            ),
        )
        row = _task_row(conn, task_id)
        conn.commit()
        return _serialize_task(row)
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/v1/tasks/{task_id}")
def get_task(task_id: str, authorization: Optional[str] = Header(default=None)):
    device = _authenticate_device(authorization, "events:read")
    try:
        task_id = str(UUID(task_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    conn = get_connection()
    try:
        row = _task_row(conn, task_id)
        _require_task_access(row, device)
        result = _serialize_task(row)
        result["updates"] = _task_updates(conn, task_id)
        return result
    finally:
        conn.close()


@app.patch("/v1/tasks/{task_id}")
def patch_task(
    task_id: str,
    payload: TaskPatch,
    authorization: Optional[str] = Header(default=None),
):
    device = _authenticate_device(authorization, "events:ingest")
    try:
        task_id = str(UUID(task_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    fields = payload.model_fields_set
    if not fields:
        raise HTTPException(status_code=422, detail="At least one field is required")
    for field in {"title", "status", "priority", "progress_percent"} & fields:
        if getattr(payload, field) is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _task_row(conn, task_id)
        _require_task_access(row, device)
        is_head = _is_project_head(device)
        if not is_head and fields - {"status", "progress_percent", "note"}:
            raise HTTPException(
                status_code=403,
                detail="Members may only report status, progress, and notes",
            )
        updates = {}
        for field in (
            "title",
            "description",
            "external_key",
            "status",
            "priority",
            "due_date",
        ):
            if field in fields:
                value = getattr(payload, field)
                updates[field] = (
                    value.isoformat() if field == "due_date" and value else value
                )
        if "assignee_id" in fields:
            public_id = payload.assignee_id
            if public_id is None:
                updates["assignee_id"] = None
            else:
                assignee = conn.execute(
                    """
                    SELECT users.id FROM users
                    JOIN project_memberships
                      ON project_memberships.user_id = users.id
                     AND project_memberships.project_id = ?
                    WHERE users.public_id = ?
                      AND project_memberships.role IN ('owner', 'member')
                    """,
                    (row["project_id"], str(public_id)),
                ).fetchone()
                if assignee is None:
                    raise HTTPException(
                        status_code=422,
                        detail="Assignee must be an owner or member of this project",
                    )
                updates["assignee_id"] = assignee["id"]
        if "external_key" in fields and payload.external_key is not None:
            duplicate_key = conn.execute(
                """
                SELECT 1 FROM tasks
                WHERE project_id = ? AND external_key = ? COLLATE NOCASE
                  AND id != ?
                """,
                (row["project_id"], payload.external_key, task_id),
            ).fetchone()
            if duplicate_key is not None:
                raise HTTPException(status_code=409, detail="Task key already exists")
        if "progress_percent" in fields:
            updates["progress_percent"] = payload.progress_percent
            updates["progress_is_estimate"] = 0

        timestamp = now_iso()
        if updates:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",
                (*updates.values(), timestamp, task_id),
            )
        if "status" in fields:
            update_type = "status.changed"
        elif "progress_percent" in fields:
            update_type = "progress.reported"
        else:
            update_type = "task.updated"
        conn.execute(
            """
            INSERT INTO task_updates
                (id, task_id, actor_id, event_id, update_type, note,
                 progress_percent, progress_is_estimate, timestamp)
            VALUES (?, ?, ?, NULL, ?, ?, ?, 0, ?)
            """,
            (
                str(uuid4()),
                task_id,
                device["user_id"],
                update_type,
                payload.note,
                payload.progress_percent if "progress_percent" in fields else None,
                timestamp,
            ),
        )
        result = _serialize_task(_task_row(conn, task_id))
        conn.commit()
        return result
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/v1/me/tasks")
def list_my_tasks(authorization: Optional[str] = Header(default=None)):
    device = _authenticate_device(authorization, "events:read")
    conn = get_connection()
    try:
        rows = _task_list_rows(
            conn,
            device["project_id"],
            " AND tasks.assignee_id = ?",
            (device["user_id"],),
        )
        return [_serialize_task(row) for row in rows]
    finally:
        conn.close()


@app.get("/v1/projects/{project_id}/activity")
def list_project_activity(
    project_id: str,
    authorization: Optional[str] = Header(default=None),
    limit: int = 100,
):
    device = _authenticate_device(authorization, "events:read")
    project_id = _project_identifier(project_id)
    _require_project_device(device, project_id)
    if not _is_project_head(device):
        raise HTTPException(
            status_code=403, detail="Only project heads can view project activity"
        )
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT event_id FROM activity_events
            WHERE project_id = ? AND source != 'compat-api'
            ORDER BY received_at DESC LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
        events = []
        for row in rows:
            event = _canonical_response(conn, row["event_id"], project_id)
            if event is not None:
                events.append(event)
        return events
    finally:
        conn.close()


@app.get("/v1/projects/{project_id}/overview")
def get_project_overview(
    project_id: str, authorization: Optional[str] = Header(default=None)
):
    device = _authenticate_device(authorization, "events:read")
    project_id = _project_identifier(project_id)
    _require_project_device(device, project_id)
    if not _is_project_head(device):
        raise HTTPException(
            status_code=403, detail="Only project heads can view project overview"
        )
    conn = get_connection()
    try:
        counts = {
            row["status"]: row["count"]
            for row in conn.execute(
                """
                SELECT status, COUNT(*) AS count FROM tasks
                WHERE project_id = ? GROUP BY status
                """,
                (project_id,),
            ).fetchall()
        }
        return {
            "project_id": project_id,
            "task_counts": {
                status: counts.get(status, 0)
                for status in ("todo", "in_progress", "blocked", "in_review", "done")
            },
            "total_tasks": sum(counts.values()),
        }
    finally:
        conn.close()


@app.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "dashboard-v2.html")


@app.get("/legacy")
def legacy_dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
