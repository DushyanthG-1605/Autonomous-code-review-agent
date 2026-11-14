# TRAC MCP

TRAC MCP is the agent-tool interface for project-aware task operations. It is an additive integration layer and does not replace the TRAC skill, collector, or backend API.

## Layer distinction

- TRAC Skill: agent behavior instructions and consent gates. It tells an AI assistant when to check tasks, report progress, and avoid transmitting sensitive content.
- TRAC Collector: local client responsible for authenticated event delivery, queueing, offline retry, pairing, and privacy filtering.
- TRAC MCP: direct tool interface for coding agents that can call task operations as structured tools.
- TRAC API: backend ingestion, persistence, real-time streaming, project access control, and task state management.

## MCP server

The MCP server lives in `trac/mcp/server.py` and exposes tools such as `trac_get_projects()`, `trac_get_tasks()`, and task lifecycle tools. All task mutation tools create valid version-2 TRAC events through the backend ingestion logic rather than bypassing the service layer.

Run it locally from the repository root:

```bash
TRAC_MCP_TOKEN="<project-scoped-device-token>" python -m trac.mcp.server --transport stdio
```

For remote or hosted transport, expose the server only behind HTTPS/TLS and require a bearer token bound to an authorized project-scoped device. Do not accept a raw long-lived secret in tool parameters.

## Authentication model

Every MCP tool call is bound to a device token already issued by the TRAC backend. The tool runtime reads the token from `TRAC_MCP_TOKEN` or `TRAC_API_TOKEN` and derives:

- the authenticated user
- the authorized project
- the valid scope set
- the task access bounds

No tool accepts a caller-supplied user identity. Cross-project or unauthorized task access fails with a structured error payload.

## Tool inventory

- `trac_get_projects()`
- `trac_get_tasks()`
- `trac_get_task(task_id)`
- `trac_start_task(task_id)`
- `trac_update_progress(task_id, percent, note)`
- `trac_report_blocker(task_id, reason)`
- `trac_complete_task(task_id, note)`
- `trac_get_project_status()`
- `trac_get_connection_status()`

Mutation tools use the same lifecycle semantics as the backend event contract:

- `task.started`
- `task.progress`
- `task.blocked`
- `task.completed`

## Compatible client configuration

The MCP server is verified for local stdio use with a standard MCP client. Do not assume every IDE supports the same config path or automatically loads a skill. Supported installation patterns are the ones that can launch a local process and pass environment variables.

### Example stdio config

```json
{
  "mcpServers": {
    "trac": {
      "command": "python",
      "args": [
        "-m",
        "trac.mcp.server",
        "--transport",
        "stdio"
      ],
      "env": {
        "TRAC_MCP_TOKEN": "<device-token>"
      }
    }
  }
}
```

### Claude Code / Cursor / Codex / VS Code / Copilot

Use the above pattern only for clients that provide a supported MCP stdio command entry. For clients that do not expose MCP configuration, use the TRAC collector and skill workflow instead. The repo does not assume that every tool supports the same path or automatic hook installation.

## Error handling

The MCP layer returns structured results such as:

```json
{
  "ok": false,
  "error": {
    "code": "task_not_found",
    "message": "Task not found or access denied"
  }
}
```

This prevents secrets or raw backend internals from being surfaced to the client.

## Reliability

The MCP server handles:

- unavailable backend or missing token
- invalid task IDs
- unauthorized project/task access
- duplicate event delivery idempotency
- revoked or expired device credentials
- network timeout or transport shutdown conditions

The underlying persistence and auth rules remain in the TRAC API and SQLite service layer.
