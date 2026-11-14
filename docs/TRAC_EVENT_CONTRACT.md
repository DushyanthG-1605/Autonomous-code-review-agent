# TRAC Event Contract

Phases 2–4 define a strict version-2 event contract, project-scoped device
credentials, project/task authorization, durable server-sent event streaming,
one-time pairing, and a shared task-event collector.

## Authentication and project access

All ingestion, list, detail, and stream routes require:

```http
Authorization: Bearer <project-scoped-device-token>
```

Tokens are random device credentials. TRAC stores only SHA-256 token hashes.
Each device is associated with one user, one project, and explicit `events:read`
and/or `events:ingest` scopes. The user must remain a member of that project.
Revoked devices, missing project membership, and missing scopes are denied.
`project_id` and `actor_id` supplied in a request are accepted only as
compatibility fields and are ignored; the credential determines both identities.

Project heads can read project activity. Members can read only their own
activity or activity attached to tasks assigned to them; task access is likewise
limited to the project head and assigned member. Project/task API details and
development login are documented in
[`TRAC_PROJECTS_TASKS.md`](TRAC_PROJECTS_TASKS.md).

Create and manage users, projects, and device tokens with:

```text
python -m trac.manage create-user <github_login>
python -m trac.manage create-project <slug> "<project name>" <owner_login>
python -m trac.manage add-project-member <slug> <github_login> [--role member|viewer]
python -m trac.manage revoke-device <device_id>
```

Owners and members receive `events:read` and `events:ingest`; viewers receive
`events:read` only. Device credentials are printed once by the management CLI.

An optional local username-based development login is available only when both
`TRAC_ENV=development` and `TRAC_DEV_AUTH_ENABLED=true` are set. It issues a
hashed project-scoped credential for an existing user. `TRAC_ENV=production`
disables the login endpoint and rejects previously issued development
credentials. This no-password mechanism is for trusted local development only.

## Version-2 event schema

`POST /v1/events` accepts one version-2 event or a batch in an `events` array.
JSON fields outside the schema are rejected.

```json
{
  "event_id": "018f5c2a-7b6d-7a22-b33a-cb1d5e12f040",
  "schema_version": 2,
  "event_type": "task.progress",
  "project_id": "018f5c2a-7b6d-7a22-b33a-cb1d5e12f042",
  "task_id": "018f5c2a-7b6d-7a22-b33a-cb1d5e12f041",
  "actor_id": "018f5c2a-7b6d-7a22-b33a-cb1d5e12f043",
  "source": "skill",
  "occurred_at": "2025-03-08T12:30:00Z",
  "metadata": {
    "progress": 60
  }
}
```

`event_id` and `task_id` must be UUIDs. `occurred_at` must include a timezone.
The server adds `received_at` and derives the persisted project and actor from
the authenticated device, not from body claims. Reusing an event ID within a
project is idempotent and produces no second event or stream record.
For task lifecycle event types, `task_id` is required and must identify an
existing task in the credential's project. Only that project's head or the
assigned member can report against it. Task lifecycle events create task
history in the same transaction as the event and stream record. GitHub commit,
pull-request, and CI evidence may also link to a task by a project-scoped
external task key; these evidence events create history but do not change task
status or progress.

### Supported event types and metadata

Metadata is an object; all unlisted properties are rejected.

| `event_type` | Allowed `metadata` |
| --- | --- |
| `task.started` | `{}` |
| `task.progress` | `progress` integer, 0–100; optional `note` (single-line, max 160 chars) |
| `task.blocked` | optional `reason_code`: `dependency`, `permission`, `test_failure`, `build_failure`, `external`, `other`; optional `note` (single-line, max 160 chars) |
| `task.completed` | required `outcome`: `done`, `partial`, `abandoned`; optional `note` (single-line, max 160 chars) |
| `git.commit` | required hexadecimal `commit_sha` (7–64 chars); optional nonnegative `files_changed`, `insertions`, `deletions`, GitHub `repository`, `branch`, and matched `task_reference` |
| `git.pull_request.opened`, `git.pull_request.updated`, `git.pull_request.merged` | positive `pull_request_number`, `base_branch`, `head_branch` (1–120 chars; letters, digits, `.`, `_`, `/`, `-`); optional GitHub `repository` and matched `task_reference` |
| `test.completed` | required `result`: `passed`, `failed`, `cancelled`; optional nonnegative `duration_ms`, `tests_run`, `tests_failed` (`tests_failed` cannot exceed `tests_run`) |
| `build.completed` | required `result`: `passed`, `failed`, `cancelled`; optional nonnegative `duration_ms`, GitHub `repository`, `branch`, `workflow_name`, positive `workflow_id`, `run_id`, `check_name`, `check_run_id`, and matched `task_reference` |
| `session.started`, `session.ended` | `{}` |

`source` is one of `skill`, `cli`, `mcp`, `github`, or `ci`.

Metadata must not contain source code, file contents, diffs, prompts, command
text, environment values, credentials, or secrets. In addition to per-event
allowlists, the API rejects forbidden field names and recognizable secret
patterns. Do not place free-form source text in fields intended for structured
activity.

`task.progress.metadata.progress` is an agent/event-reported percentage and is
stored on the task with `progress_is_estimate=true`; TRAC does not infer exact
progress from elapsed time or commit counts. An explicit successful
`task.completed` event sets state to `done` and progress to 100. Direct human
task API progress reports are stored with `progress_is_estimate=false`.
Task notes are optional short summaries. The collector redacts recognizable
secrets locally; notes must never contain source code, prompts, diffs, paths,
command text, credentials, or other sensitive content.

## API

| Method and path | Description |
| --- | --- |
| `POST /v1/events` | Authenticated single-event or batch ingestion. Version-2 schema failures return `422`; duplicates are counted and not reinserted. |
| `GET /v1/events?limit=100` | Newest project events; `limit` must be 1–200. |
| `GET /v1/events/{event_id}` | Get one UUID event within the credential's project; events outside the project return `404`. |
| `GET /v1/events/stream` | Authenticated project event stream. |
| `GET /api/events` | Authenticated compatibility event feed used by the dashboard. |
| `GET /api/events/{integer_id}` | Authenticated, project-scoped legacy detail route. |
| `POST /api/events` | Authenticated compatibility ingestion for the existing review reporter. Body and response retain the legacy event shape; caller-supplied actor identity is ignored. |
| `GET /api/events/stream` | Compatibility alias for the authenticated SSE stream. |
| `GET /v1/projects`, `POST /v1/projects`, `GET /v1/projects/{id}` | Project membership and creation; project creation returns a project-scoped token once. |
| `GET/POST /v1/projects/{id}/tasks` | Authorized task listing and project-head task creation. |
| `GET/PATCH /v1/tasks/{id}` | Read task/history or update it as project head/assignee. |
| `GET /v1/me/tasks` | Assigned tasks in the current credential's project. |
| `GET /v1/projects/{id}/activity` | Project-head-only activity view. |
| `GET /v1/projects/{id}/overview` | Project-head-only task counts. |
| `POST /v1/integrations/github/webhook` | HMAC-SHA256 verified GitHub delivery; repository mapping and delivery ID determine project and idempotency. |
| `POST /v1/dev/login` | Optional local login, disabled unless explicitly enabled for development. |
| `POST /v1/pairings` | Authenticated pairing issuance; creator must hold `events:ingest`, and the project/scopes are restricted to the creator's credential. |
| `POST /v1/pairings/claim` | Rate-limited, one-time pairing claim; creates a device only after successful claim. |
| `GET /v1/me/device`, `DELETE /v1/me/device` | Inspect the authenticated device identity/scopes or revoke that device. |

### Device pairing

An authenticated project device can create a scoped one-time code:

```http
POST /v1/pairings
Authorization: Bearer <authorized-device-token>
Content-Type: application/json

{
  "project_id": "018f5c2a-7b6d-7a22-b33a-cb1d5e12f042",
  "scopes": ["events:read", "events:ingest"]
}
```

The `201` response contains `pairing_id`, a random `pairing_code`, `expires_at`
(ten minutes after issuance), and the granted `scopes`. The server stores only
the code's SHA-256 hash. Issuance is limited to five requests per issuer per
hour; claim attempts are limited to ten per client per minute. Claim with:

```http
POST /v1/pairings/claim
Content-Type: application/json

{"code": "<one-time-code>", "device_label": "TRAC CLI"}
```

The successful claim response contains a one-time `access_token`, device ID,
project ID, user ID, and scopes. A code is single-use, expires after ten
minutes, and is invalidated when its issuing device is revoked. New device
credentials are stored only as SHA-256 hashes. `DELETE /v1/me/device` revokes
the authenticated device; the collector's `trac disconnect` command also
clears its local pending queue.

Successful POST responses use the existing collector-compatible shape:

```json
{"accepted": 1, "duplicates": 0, "rejected": []}
```

Version-2 ingestion also accepts the existing version-1 collector envelope
inside `{"events": [...]}`. Version 1 retains its ULID ID and legacy event
names/data allowlists; unsupported or invalid records are returned in
`rejected`. Legacy source values `git-hook` and `claude-hook` are normalized to
`cli`. This compatibility parser does not expand the version-2 metadata
allowlists.

## Server-sent events

The stream response is `text/event-stream`. It sends `retry: 3000`, then
project-authorized `activity` frames:

```text
id: 018f5c2a-7b6d-7a22-b33a-cb1d5e12f040
event: activity
data: {"event_id":"018f5c2a-7b6d-7a22-b33a-cb1d5e12f040","schema_version":2,...}
```

The frame ID is the event UUID. Clients reconnect with the standard
`Last-Event-ID` request header; only records after that cursor are replayed.
Unknown or cross-project cursors return `400`. A newly opened stream without a
cursor starts at the latest sequence and receives subsequent events only.
`Last-Event-ID: 0` is a beginning-of-project cursor for a first sync. The
authenticated event-list routes return `X-TRAC-Event-Cursor` from the same
database snapshot as the list; use that value when opening the stream to avoid
a list/stream race. The dashboard follows this snapshot-cursor pattern.
Heartbeat comments are sent every 15 seconds when idle. Disconnects terminate
the generator; revoked or no-longer-authorized devices are closed on the next
poll.

The legacy `POST /api/events` route also enters the durable project stream as
an `event: compatibility` frame. This preserves delivery for the existing
review reporter without treating its legacy event types as version-2 event
types. These compatibility-only records are not returned by `GET /v1/events`.

Durability comes from the SQLite `activity_event_stream` table, which stores a
monotonic sequence for every persisted activity event. The SSE handler polls
this project-filtered table every 0.5 seconds, so replay works across server
restarts and does not depend on process-local pub/sub. With SQLite, a single
database file is shared by the app's local processes; multi-host deployment
requires shared transactional storage or an external event bus and is not
provided by this phase.

The browser dashboard uses `fetch` streaming because native `EventSource` cannot
set the required bearer authorization header. It keeps its token in
`sessionStorage` only, reacts to both event frame types by refreshing the
project feed, and uses the SSE cursor to resume.

## Database migration

Startup applies the additive
`trac/migrations/001_project_scoped_events.sql`,
`trac/migrations/002_projects_tasks_identity.sql`, and
`trac/migrations/003_device_pairing.sql`, and
`trac/migrations/004_github_ci_integration.sql` migrations tracked in
`schema_migrations`. Migration 001 adds public actor IDs, projects,
memberships, device project/scopes, event project references, and the durable
stream sequence. Migration 002 adds optional user email/GitHub identity,
project key/description/head, task and task-update tables, and development
credential mode. Migration 003 adds hashed one-time device pairings and
persistent pairing rate-limit buckets. Migration 004 adds project-scoped task
external keys, explicit GitHub repository mappings, and a unique webhook
delivery ledger. Existing project slugs and owner memberships are used to
backfill keys and heads; existing event and review records are preserved.

GitHub signature verification, repository mapping, supported event actions,
and integration limitations are specified in
[`TRAC_GITHUB_INTEGRATION.md`](TRAC_GITHUB_INTEGRATION.md).
