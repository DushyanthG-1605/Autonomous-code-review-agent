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
the SSE stream. The engineering intelligence dashboard is served at `/`; the
previous dashboard remains available at `/legacy`. Project credentials entered
in the new dashboard are kept only in the current browser session. Connect one or
more project-scoped credentials to load only authorized project data. Event
batches are limited to 100 records and
256 KB, with a per-device limit of 120 requests per minute. See
[`docs/TRAC_EVENT_CONTRACT.md`](docs/TRAC_EVENT_CONTRACT.md) for the version-2
contract, compatibility behavior, and stream protocol. Project/task APIs,
member permissions, task progress estimates, and the opt-in local development
login are described in [`docs/TRAC_PROJECTS_TASKS.md`](docs/TRAC_PROJECTS_TASKS.md).

The review reporter uses `TRAC_EVENTS_URL` and `TRAC_API_TOKEN`; configure the
token through the process environment rather than source code:

```powershell
$env:TRAC_API_TOKEN = "<project-scoped-device-token>"
$env:TRAC_EVENTS_URL = "http://127.0.0.1:8000/api/events"
```

## GitHub and CI activity

TRAC can receive signed GitHub webhooks at
`POST /v1/integrations/github/webhook`. Configure
`TRAC_GITHUB_WEBHOOK_SECRET` in the server's secret manager, then explicitly map
each repository to a TRAC project:

```text
python -m trac.manage map-github-repo <project-slug> <owner/repository>
python -m trac.manage unmap-github-repo <owner/repository>
```

GitHub push, pull request, workflow-run, and check-run evidence uses the same
canonical activity store and project-scoped activity API as agent events. See
[`docs/TRAC_GITHUB_INTEGRATION.md`](docs/TRAC_GITHUB_INTEGRATION.md) for
signature setup, supported actions, task-key matching, and limitations.

Revoke a device with:

```text
python -m trac.manage revoke-device <id>
```

## Install the TRAC collector

From the repository root, install the shared collector and place the universal
skill in the current agent's documented project skill directory:

```text
pip install -e collector
trac init --target <claude|cursor|codex|copilot>
trac login --url <server>
trac status
```

Ask the developer for explicit consent before enabling tracking or running
task-reporting commands. A project member/head with event-ingest permission
creates a short-lived pairing code from `POST /v1/pairings`; `trac login`
claims it without echoing the code. Long-lived device credentials are stored
in the OS credential store. See
[`docs/TRAC_UNIVERSAL_INTEGRATION.md`](docs/TRAC_UNIVERSAL_INTEGRATION.md)
for pairing, compatibility, CLI, queue, and privacy details.

Task reporting uses assigned task UUIDs and the version-2 event contract. The
collector never captures source code, diffs, prompts, command text, environment
values, or file contents. Pause with `trac pause`, inspect redacted event data
with `trac data`, resume with `trac resume`, and revoke the device plus clear
queued events with `trac disconnect`. Optional Git hooks require the explicit
`trac init --git-hooks` flag; they are not required for task reporting.
