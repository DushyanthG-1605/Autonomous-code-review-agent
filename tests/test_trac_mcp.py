import asyncio
import json
import os
import sys
import uuid

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from trac import db
from trac.models import now_iso


@pytest.fixture
def mcp_project_state(tmp_path, monkeypatch):
    db_path = tmp_path / "mcp-test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setenv("TRAC_DB_PATH", str(db_path))
    db.init_db()

    def build(token: str, project_slug: str, project_name: str, user_login: str):
        conn = db.get_connection()
        user = conn.execute(
            """
            INSERT INTO users (github_login, display_name, created_at, public_id)
            VALUES (?, ?, ?, ?)
            """,
            (user_login, user_login.title(), now_iso(), str(uuid.uuid4())),
        )
        user_id = user.lastrowid
        project_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO projects (id, slug, name, project_key, project_head_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, project_slug, project_name, project_slug, user_id, now_iso()),
        )
        conn.execute(
            """
            INSERT INTO project_memberships (project_id, user_id, role, created_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (project_id, user_id, now_iso()),
        )
        conn.execute(
            """
            INSERT INTO devices (user_id, token_hash, label, created_at, project_id, scopes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                __import__("hashlib").sha256(token.encode("utf-8")).hexdigest(),
                f"{user_login} device",
                now_iso(),
                project_id,
                '["events:read","events:ingest"]',
            ),
        )
        task_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO tasks
                (id, project_id, title, description, assignee_id, status, priority,
                 progress_percent, progress_is_estimate, due_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'todo', 'medium', 0, 0, NULL, ?, ?)
            """,
            (task_id, project_id, "Implement auth", "Describe the auth flow", user_id, now_iso(), now_iso()),
        )
        conn.commit()
        conn.close()
        return token, project_id, task_id

    return build


def _tool_payload(result):
    if hasattr(result, "content") and result.content:
        first = result.content[0]
        if hasattr(first, "text"):
            text = first.text
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": text}
    if hasattr(result, "structuredContent"):
        return result.structuredContent
    return result


async def _call_tool(token: str, tool_name: str, arguments: dict):
    env = os.environ.copy()
    env["TRAC_MCP_TOKEN"] = token
    env["TRAC_DB_PATH"] = os.environ["TRAC_DB_PATH"]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "trac.mcp.server", "--transport", "stdio"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return _tool_payload(result)


@pytest.mark.asyncio
async def test_mcp_discovers_tools_and_reads_task_state(mcp_project_state):
    token, project_id, task_id = mcp_project_state(
        "mcp-device-token",
        "alpha-project",
        "Alpha Project",
        "alice",
    )

    async with stdio_client(
        StdioServerParameters(
            command=sys.executable,
            args=["-m", "trac.mcp.server", "--transport", "stdio"],
            env={**os.environ, "TRAC_MCP_TOKEN": token},
        )
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            available = await session.list_tools()
            names = {tool.name for tool in available.tools}
            assert {"trac_get_projects", "trac_get_tasks", "trac_get_task", "trac_get_project_status"}.issubset(names)

            tasks_payload = _tool_payload(await session.call_tool("trac_get_tasks", {}))
            assert tasks_payload["ok"] is True
            assert any(task["id"] == task_id for task in tasks_payload["tasks"])

            task_payload = _tool_payload(await session.call_tool("trac_get_task", {"task_id": task_id}))
            assert task_payload["ok"] is True
            assert task_payload["task"]["id"] == task_id
            assert task_payload["task"]["project_id"] == project_id


@pytest.mark.asyncio
async def test_mcp_task_lifecycle_updates_backend_and_event_history(mcp_project_state):
    token, project_id, task_id = mcp_project_state(
        "mcp-task-token",
        "beta-project",
        "Beta Project",
        "bob",
    )

    payload = await _call_tool(token, "trac_start_task", {"task_id": task_id})
    assert payload["ok"] is True
    assert payload["duplicate"] is False

    payload = await _call_tool(token, "trac_update_progress", {"task_id": task_id, "percent": 50, "note": "Auth flow drafted"})
    assert payload["ok"] is True

    payload = await _call_tool(token, "trac_report_blocker", {"task_id": task_id, "reason": "Waiting on repo permissions"})
    assert payload["ok"] is True

    payload = await _call_tool(token, "trac_complete_task", {"task_id": task_id, "note": "Authentication verified"})
    assert payload["ok"] is True

    conn = db.get_connection()
    try:
        task = conn.execute("SELECT status, progress_percent FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert task["status"] == "done"
        assert task["progress_percent"] == 100
        event_count = conn.execute("SELECT COUNT(*) FROM activity_events WHERE task_id = ?", (task_id,)).fetchone()[0]
        update_count = conn.execute("SELECT COUNT(*) FROM task_updates WHERE task_id = ?", (task_id,)).fetchone()[0]
        assert event_count >= 4
        assert update_count >= 4
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_mcp_unauthorized_project_access_returns_error(mcp_project_state):
    first_token, _, task_id = mcp_project_state(
        "owner-token",
        "gamma-project",
        "Gamma Project",
        "carol",
    )
    other_token, _, _ = mcp_project_state(
        "other-token",
        "delta-project",
        "Delta Project",
        "dave",
    )

    payload = await _call_tool(other_token, "trac_get_task", {"task_id": task_id})
    assert payload["ok"] is False
    assert payload["error"]["code"] in {"task_not_found", "validation_error"}

    payload = await _call_tool(first_token, "trac_get_connection_status", {})
    assert payload["ok"] is True
    assert payload["project_id"]
