# TRAC Projects, Tasks, and Developer Identity

Phase 3 adds project ownership, task management, developer identity fields, and
task-linked event history on top of the Phase 2 event pipeline. It preserves
the existing FastAPI/SQLite implementation and applies migration
`002_projects_tasks_identity.sql` additively.

## Identity and authorization

- `users.id` remains the internal integer key; APIs expose `users.public_id`.
- `github_login` remains for compatibility. `github_username` and `email` are
  optional fields; `role` defaults to `developer`.
- `projects.project_key` is unique. Existing `slug` values are copied into it.
- `projects.project_head_id` identifies the project head. Existing project
  owners are backfilled as heads; project membership role `owner` is retained
  for compatibility.
- Device credentials remain scoped to one project and store only a SHA-256
  hash. The authenticated credential determines the actor and project;
  request-supplied `actor_id` and `project_id` are ignored.
- A project head can create and assign tasks and see all project tasks and
  activity. A member can see only tasks assigned to them, update those tasks,
  and read their own events or events attached to their assigned tasks.
  Project-wide activity and overview are head-only. Other-project resources
  are returned as `404`.
- Assignment is limited to project owners and members, not viewers.

### Local development login

Project-scoped management tokens continue to work in all environments. For
local development only, the optional username-based development login can be
enabled by setting both:

```powershell
$env:TRAC_ENV = "development"
$env:TRAC_DEV_AUTH_ENABLED = "true"
```

Then call `POST /v1/dev/login` with an existing user and project:

```json
{
  "github_username": "alice",
  "project_id": "018f5c2a-7b6d-7a22-b33a-cb1d5e12f042"
}
```

The endpoint returns a bearer credential (the server stores only its hash).
The credential remains valid until revoked or development auth is disabled.
This development-only login has no password and must be used only against a
trusted local development server. It is unavailable unless explicitly enabled
with `TRAC_ENV=development`; setting `TRAC_ENV=production` disables both the
login endpoint and previously issued development credentials, even if
`TRAC_DEV_AUTH_ENABLED` remains set.

## Task data model

`tasks` contains:

| Field | Meaning |
| --- | --- |
| `id` | UUID task identifier |
| `project_id` | Owning project |
| `title`, `description` | Task text |
| `external_key` | Optional project-scoped key for explicit GitHub task association, e.g. `TRAC-101` |
| `assignee_id` | Optional internal user foreign key |
| `status` | `todo`, `in_progress`, `blocked`, `in_review`, or `done` |
| `priority` | `low`, `medium`, `high`, or `urgent` |
| `progress_percent` | Explicitly reported integer from 0 to 100 |
| `progress_is_estimate` | Whether the percentage is an agent/event estimate |
| `due_date` | Optional ISO date |
| `created_at`, `updated_at` | UTC timestamps |

`task_updates` records each API update and each accepted event associated with
a task. Event-origin updates carry the persisted `event_id` and the
credential-derived actor. Direct task PATCH updates have a null `event_id`
because they are not event ingestion; they still record `update_type`, `note`,
actor, and timestamp.

Task progress is never derived from elapsed time or commit count. A
`task.progress` event writes the reported percentage with
`progress_is_estimate=true`. A successful explicit `task.completed` event with
`outcome=done` sets the task status to `done` and progress to 100 as a
completion outcome, not as a time- or commit-based estimate. A direct API
progress report is stored as non-estimated.

## API

All routes below except `POST /v1/dev/login` require
`Authorization: Bearer <project-scoped-device-token>`. Project resource routes
require a credential scoped to that project.

| Method and path | Access and behavior |
| --- | --- |
| `GET /v1/projects` | List the authenticated user's project memberships. |
| `POST /v1/projects` | Create a project; caller becomes project head. Returns its project-scoped device token once. |
| `GET /v1/projects/{id}` | Get membership-visible project details. |
| `GET /v1/projects/{id}/tasks` | Heads see all tasks; members see assigned tasks only. |
| `POST /v1/projects/{id}/tasks` | Project head creates and optionally assigns a task. New tasks start in `todo` at 0%. |
| `GET /v1/tasks/{id}` | Project head or assigned member gets task and update history. |
| `PATCH /v1/tasks/{id}` | Head edits task fields; assigned member can report status, progress, and a note. |
| `GET /v1/me/tasks` | Assigned tasks in the credential's project. |
| `GET /v1/me/device` | Current credential identity, project, scopes, and last event timestamp. |
| `GET /v1/projects/{id}/devices` | Project head only; authorized devices with scopes and last event attribution. |
| `DELETE /v1/projects/{id}/devices/{device_id}` | Project head only; revoke a device in that project. |
| `GET /v1/pairings/{pairing_id}` | Pairing issuer only; reports pending, claimed, expired, or revoked status. |
| `GET /v1/projects/{id}/activity` | Project head only; authorized project event feed. |
| `GET /v1/projects/{id}/overview` | Project head only; task counts by state. |
| `POST /v1/dev/login` | No-password local login, disabled unless explicitly enabled for development. |

Dashboard device attribution and pairing claim linkage are added by the
additive `005_dashboard_device_activity.sql` migration. Historical events keep
a null device attribution; new device-originated events record the authenticated
device ID. GitHub/CI events are server-originated and remain unattributed to a
device.

### Create a project

```http
POST /v1/projects
Authorization: Bearer <existing-token>
Content-Type: application/json
```

```json
{
  "name": "Payments",
  "description": "Payments service engineering work",
  "project_key": "payments"
}
```

The response contains the project metadata and a one-time `device_token`
scoped to the new project. Store it securely; later reads reveal only the
stored token hash. Reusing an existing `project_key` returns `409`.

### Create and assign a task

```http
POST /v1/projects/018f5c2a-7b6d-7a22-b33a-cb1d5e12f042/tasks
Authorization: Bearer <project-head-token>
Content-Type: application/json
```

```json
{
  "title": "Add payment retry handling",
  "description": "Retry transient provider failures.",
  "external_key": "TRAC-101",
  "assignee_id": "018f5c2a-7b6d-7a22-b33a-cb1d5e12f043",
  "priority": "high",
  "due_date": "2025-04-15"
}
```

The `assignee_id` is the user's public UUID and must identify an owner/member
of the same project. A task response includes its state, progress percentage,
estimate flag, assignee public ID, and timestamps.

### Update a task

An assigned member may report progress without claiming an exact measurement:

```http
PATCH /v1/tasks/018f5c2a-7b6d-7a22-b33a-cb1d5e12f044
Authorization: Bearer <assigned-member-token>
Content-Type: application/json
```

```json
{
  "status": "in_progress",
  "progress_percent": 45,
  "note": "Implementation underway"
}
```

The response reports `progress_is_estimate: false` for this explicit human API
update. Values outside 0–100 and unknown status/priority values return `422`.
Members attempting to edit assignment, title, description, priority, or due
date receive `403`; unassigned members cannot view or update the task.

## Task events and history

Version-2 task event types (`task.started`, `task.progress`, `task.blocked`,
`task.completed`) require a `task_id` that exists in the credential's project.
Only the project head or the assigned member can report against that task.
Accepted events update task state and create exactly one `task_updates` row in
the same transaction as the event and stream record. Replayed event IDs create
no additional history. GitHub commit, pull-request, or CI evidence may also
create a task history row when its explicit reference matches a task's
`external_key`; these evidence events do not change task status or progress.

```json
{
  "event_id": "018f5c2a-7b6d-7a22-b33a-cb1d5e12f045",
  "schema_version": 2,
  "event_type": "task.progress",
  "task_id": "018f5c2a-7b6d-7a22-b33a-cb1d5e12f044",
  "source": "mcp",
  "occurred_at": "2025-03-08T12:30:00Z",
  "metadata": {
    "progress": 60
  }
}
```

The authenticated credential supplies the actor and project; body claims do
not override either. The task detail response exposes the resulting update's
`event_id`, actor public ID, reported progress and estimate flag.

Version-1 collector events remain accepted for compatibility. That envelope
does not carry a structured task UUID, so legacy task events are not attached
to a `tasks` record. New task lifecycle producers must emit version 2 with a
real task ID.

## Migration

`002_projects_tasks_identity.sql` adds optional developer identity fields,
device auth mode, project description/key/head, task and task-update tables,
foreign keys, and lookup indexes. It backfills GitHub usernames from the
existing login, project keys from slugs, and heads from existing owner
memberships. Historical activity rows and review events are preserved.
