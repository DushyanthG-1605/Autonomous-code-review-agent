import argparse
import asyncio
import json
import os
import uuid
from typing import Any, Dict, Optional

from mcp.server.mcpserver import MCPServer

from trac.db import get_connection, init_db
from trac.main import (
    _authenticate_device,
    _credential_scopes,
    _ingest_canonical,
    _is_project_head,
    _project_response,
    _serialize_task,
    _task_row,
    _task_updates,
    now_iso,
)
from trac.models import CanonicalEvent


def _mcp_auth_required(required_scope: str):
    token = os.getenv("TRAC_MCP_TOKEN") or os.getenv("TRAC_API_TOKEN")
    if not token:
        raise PermissionError(
            "No TRAC MCP credential is configured. Set TRAC_MCP_TOKEN or TRAC_API_TOKEN."
        )
    try:
        return _authenticate_device(f"Bearer {token}", required_scope)
    except Exception as exc:  # pragma: no cover - exercised via MCP client tests
        raise PermissionError("TRAC MCP authentication failed") from exc


def _structured_error(message: str, code: str = "mcp_error") -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _normalize_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:  # pragma: no cover - validated by pydantic
        raise ValueError(f"Invalid task id: {value}") from exc


def _load_projects_for_user(user_id: int):
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT projects.*, users.public_id AS head_public_id,
                   project_memberships.role AS membership_role
            FROM project_memberships
            JOIN projects ON projects.id = project_memberships.project_id
            LEFT JOIN users ON users.id = projects.project_head_id
            WHERE project_memberships.user_id = ?
            ORDER BY projects.name COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()
        return [_project_response(row) for row in rows]
    finally:
        conn.close()


def _load_project_tasks(project_id: str, user_id: int, is_head: bool):
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT tasks.*, assignee.public_id AS assignee_public_id,
                   assignee.display_name AS assignee_display_name,
                   assignee.github_username AS assignee_github_username
            FROM tasks
            LEFT JOIN users AS assignee ON assignee.id = tasks.assignee_id
            WHERE tasks.project_id = ?
              AND (? OR tasks.assignee_id = ?)
            ORDER BY tasks.updated_at DESC, tasks.created_at DESC
            """,
            (project_id, int(is_head), user_id),
        ).fetchall()
        return [_serialize_task(row) for row in rows]
    finally:
        conn.close()


def _load_task(task_id: str, device):
    conn = get_connection()
    try:
        row = _task_row(conn, task_id)
        if row is None or row["project_id"] != device["project_id"]:
            return None
        if not (_is_project_head(device) or row["assignee_id"] == device["user_id"]):
            return None
        result = _serialize_task(row)
        result["updates"] = _task_updates(conn, task_id)
        return result
    finally:
        conn.close()


async def _persist_task_event(device, task_id: str, event_type: str, metadata: Dict[str, Any]):
    conn = get_connection()
    try:
        task = _task_row(conn, task_id)
        if task is None or task["project_id"] != device["project_id"]:
            raise ValueError("Task not found for this project")
        if not (_is_project_head(device) or task["assignee_id"] == device["user_id"]):
            raise PermissionError("Not authorized to report on this task")

        event_payload = {
            "event_id": str(uuid.uuid4()),
            "schema_version": 2,
            "event_type": event_type,
            "project_id": str(device["project_id"]),
            "task_id": str(task_id),
            "actor_id": str(device["actor_id"]),
            "source": "mcp",
            "occurred_at": now_iso(),
            "metadata": metadata,
        }
        event = CanonicalEvent.model_validate(event_payload)
        inserted = _ingest_canonical(conn, event, device, now_iso())
        conn.commit()
        if not inserted:
            return {"ok": True, "event_id": str(event.event_id), "duplicate": True, "task": _serialize_task(_task_row(conn, task_id))}
        return {"ok": True, "event_id": str(event.event_id), "duplicate": False, "task": _serialize_task(_task_row(conn, task_id))}
    finally:
        conn.close()


mcp = MCPServer(
    "TRAC",
    version="0.5.0",
    instructions=(
        "Use TRAC MCP only for project-scoped task tracking. "
        "Never send prompts, file contents, source code, JWTs, tokens, or credentials. "
        "Each tool call is bound to the authenticated TRAC device identity."
    ),
)


@mcp.tool(name="trac_get_projects")
async def trac_get_projects() -> Dict[str, Any]:
    try:
        device = _mcp_auth_required("events:read")
        return {"ok": True, "projects": _load_projects_for_user(device["user_id"])}
    except PermissionError as exc:
        return _structured_error(str(exc), code="authentication_error")
    except Exception as exc:  # pragma: no cover - unexpected runtime guard
        return _structured_error(f"Unable to load projects: {exc}", code="project_load_error")


@mcp.tool(name="trac_get_tasks")
async def trac_get_tasks() -> Dict[str, Any]:
    try:
        device = _mcp_auth_required("events:read")
        return {
            "ok": True,
            "tasks": _load_project_tasks(device["project_id"], device["user_id"], _is_project_head(device)),
            "project_id": device["project_id"],
        }
    except PermissionError as exc:
        return _structured_error(str(exc), code="authentication_error")
    except Exception as exc:  # pragma: no cover - unexpected runtime guard
        return _structured_error(f"Unable to load tasks: {exc}", code="task_load_error")


@mcp.tool(name="trac_get_task")
async def trac_get_task(task_id: str) -> Dict[str, Any]:
    try:
        device = _mcp_auth_required("events:read")
        task_uuid = _normalize_uuid(task_id)
        task = _load_task(task_uuid, device)
        if task is None:
            return _structured_error("Task not found or access denied", code="task_not_found")
        return {"ok": True, "task": task}
    except (PermissionError, ValueError) as exc:
        return _structured_error(str(exc), code="validation_error" if isinstance(exc, ValueError) else "authentication_error")
    except Exception as exc:  # pragma: no cover - unexpected runtime guard
        return _structured_error(f"Unable to load task: {exc}", code="task_load_error")


@mcp.tool(name="trac_start_task")
async def trac_start_task(task_id: str) -> Dict[str, Any]:
    try:
        device = _mcp_auth_required("events:ingest")
        task_uuid = _normalize_uuid(task_id)
        return await _persist_task_event(device, task_uuid, "task.started", {})
    except (PermissionError, ValueError) as exc:
        return _structured_error(str(exc), code="validation_error" if isinstance(exc, ValueError) else "authentication_error")
    except Exception as exc:  # pragma: no cover - unexpected runtime guard
        return _structured_error(f"Unable to start task: {exc}", code="task_update_error")


@mcp.tool(name="trac_update_progress")
async def trac_update_progress(task_id: str, percent: int, note: Optional[str] = None) -> Dict[str, Any]:
    try:
        if not 0 <= int(percent) <= 100:
            raise ValueError("Percent must be between 0 and 100")
        device = _mcp_auth_required("events:ingest")
        task_uuid = _normalize_uuid(task_id)
        payload = {"progress": int(percent)}
        if note is not None:
            payload["note"] = note[:160]
        return await _persist_task_event(device, task_uuid, "task.progress", payload)
    except (PermissionError, ValueError) as exc:
        return _structured_error(str(exc), code="validation_error" if isinstance(exc, ValueError) else "authentication_error")
    except Exception as exc:  # pragma: no cover - unexpected runtime guard
        return _structured_error(f"Unable to update progress: {exc}", code="task_update_error")


@mcp.tool(name="trac_report_blocker")
async def trac_report_blocker(task_id: str, reason: str) -> Dict[str, Any]:
    try:
        device = _mcp_auth_required("events:ingest")
        task_uuid = _normalize_uuid(task_id)
        payload = {"reason_code": "other", "note": reason[:160]}
        return await _persist_task_event(device, task_uuid, "task.blocked", payload)
    except (PermissionError, ValueError) as exc:
        return _structured_error(str(exc), code="validation_error" if isinstance(exc, ValueError) else "authentication_error")
    except Exception as exc:  # pragma: no cover - unexpected runtime guard
        return _structured_error(f"Unable to report blocker: {exc}", code="task_update_error")


@mcp.tool(name="trac_complete_task")
async def trac_complete_task(task_id: str, note: Optional[str] = None) -> Dict[str, Any]:
    try:
        device = _mcp_auth_required("events:ingest")
        task_uuid = _normalize_uuid(task_id)
        payload = {"outcome": "done"}
        if note is not None:
            payload["note"] = note[:160]
        return await _persist_task_event(device, task_uuid, "task.completed", payload)
    except (PermissionError, ValueError) as exc:
        return _structured_error(str(exc), code="validation_error" if isinstance(exc, ValueError) else "authentication_error")
    except Exception as exc:  # pragma: no cover - unexpected runtime guard
        return _structured_error(f"Unable to complete task: {exc}", code="task_update_error")


@mcp.tool(name="trac_get_project_status")
async def trac_get_project_status() -> Dict[str, Any]:
    try:
        device = _mcp_auth_required("events:read")
        conn = get_connection()
        try:
            counts = {
                row["status"]: row["count"]
                for row in conn.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM tasks
                    WHERE project_id = ? GROUP BY status
                    """,
                    (device["project_id"],),
                ).fetchall()
            }
            return {
                "ok": True,
                "project_id": device["project_id"],
                "task_counts": {status: counts.get(status, 0) for status in ("todo", "in_progress", "blocked", "in_review", "done")},
                "total_tasks": sum(counts.values()),
            }
        finally:
            conn.close()
    except PermissionError as exc:
        return _structured_error(str(exc), code="authentication_error")
    except Exception as exc:  # pragma: no cover - unexpected runtime guard
        return _structured_error(f"Unable to load project status: {exc}", code="project_status_error")


@mcp.tool(name="trac_get_connection_status")
async def trac_get_connection_status() -> Dict[str, Any]:
    try:
        device = _mcp_auth_required("events:read")
        return {
            "ok": True,
            "connected": True,
            "project_id": device["project_id"],
            "project_key": device["project_key"],
            "user_id": device["actor_id"],
            "scopes": _credential_scopes(device),
            "transport": "stdio",
        }
    except PermissionError as exc:
        return _structured_error(str(exc), code="authentication_error")
    except Exception as exc:  # pragma: no cover - unexpected runtime guard
        return _structured_error(f"Unable to read connection status: {exc}", code="connection_status_error")


async def _run_server(transport: str, host: str, port: int) -> None:
    init_db()
    if transport == "stdio":
        await mcp.run_stdio_async()
        return
    if transport == "sse":
        await mcp.run_sse_async(host=host, port=port)
        return
    if transport == "streamable-http":
        await mcp.run_streamable_http_async(host=host, port=port)
        return
    raise ValueError(f"Unsupported transport: {transport}")


def build_mcp_server():
    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TRAC Model Context Protocol server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(_run_server(args.transport, args.host, args.port))


if __name__ == "__main__":
    main()
