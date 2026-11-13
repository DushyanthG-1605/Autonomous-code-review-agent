# Autonomous-code-review-agent
LangGraph-based agent that autonomously reviews GitHub PRs for bugs, security issues, code quality, and maintainability using repo-aware RAG

## TRAC event ingestion

The TRAC backend applies additive SQLite migrations on startup. Create a user with a
personal project and a project-scoped device token:

```text
python -m trac.manage create-user <github_login>
```

To create a shared project, first create its members and then assign the project:

```text
python -m trac.manage create-user <owner_login>
python -m trac.manage create-user <member_login>
python -m trac.manage create-project <slug> "<project name>" <owner_login>
python -m trac.manage add-project-member <slug> <member_login>
```

Each command prints a device token once; only its SHA-256 hash is stored. The
token is scoped to one project and server-side permissions. Use it as a bearer
credential for `/v1/events` ingestion, project-filtered `/v1/events` reads, and
the SSE stream. The dashboard's **Set API token** button stores the token only
in the current browser session. Event batches are limited to 100 records and
256 KB, with a per-device limit of 120 requests per minute. See
[`docs/TRAC_EVENT_CONTRACT.md`](docs/TRAC_EVENT_CONTRACT.md) for the version-2
contract, compatibility behavior, and stream protocol.

The review reporter uses `TRAC_EVENTS_URL` and `TRAC_API_TOKEN`; configure the
token through the process environment rather than source code:

```powershell
$env:TRAC_API_TOKEN = "<project-scoped-device-token>"
$env:TRAC_EVENTS_URL = "http://127.0.0.1:8000/api/events"
```

Revoke a device with:

```text
python -m trac.manage revoke-device <id>
```

## Install the TRAC activity tracker

From the repository root, install the collector and configure a repository:

```text
pip install -e collector
trac login --url <server> --token <device-token>
trac init
trac init --claude  # Optional: merge Claude Code hooks into .claude/settings.json
trac status
```

The tracker reports task summaries and outcomes plus session boundaries. Git
hooks report commit statistics and branch/push metadata. Optional Claude hooks
report edited-file paths, language and line counts; command kind and exit code;
and a prompt-submission count. TRAC never collects source or file contents,
diffs, prompt text, shell-command text, environment values, secrets, or commit
messages.

Pause collection immediately with `trac pause`; inspect queued and recently
sent metadata with `trac data`; resume with `trac resume`. Offline events stay
queued for `trac flush` when the server is reachable. The progress-reporting
skill is in [`skill/trac-activity/SKILL.md`](skill/trac-activity/SKILL.md).
Claude hook settings can also be viewed in
[`collector/examples/.claude/settings.json`](collector/examples/.claude/settings.json).
