# TRAC Event Contract

Phase 2 defines a strict version-2 event contract, project-scoped device
credentials, authenticated reads, and a durable server-sent event stream.

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

Create and manage users, projects, and device tokens with:

```text
python -m trac.manage create-user <github_login>
python -m trac.manage create-project <slug> "<project name>" <owner_login>
python -m trac.manage add-project-member <slug> <github_login> [--role member|viewer]
python -m trac.manage revoke-device <device_id>
```

Owners and members receive `events:read` and `events:ingest`; viewers receive
`events:read` only. Device credentials are printed once by the management CLI.

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

### Supported event types and metadata

Metadata is an object; all unlisted properties are rejected.

| `event_type` | Allowed `metadata` |
| --- | --- |
| `task.started` | `{}` |
| `task.progress` | `progress` integer, 0–100 |
| `task.blocked` | optional `reason_code`: `dependency`, `permission`, `test_failure`, `build_failure`, `external`, `other` |
| `task.completed` | required `outcome`: `done`, `partial`, `abandoned` |
| `git.commit` | required hexadecimal `commit_sha` (7–64 chars); optional nonnegative `files_changed`, `insertions`, `deletions` |
| `git.pull_request.opened`, `git.pull_request.merged` | positive `pull_request_number`, `base_branch`, `head_branch` (1–120 chars; letters, digits, `.`, `_`, `/`, `-`) |
| `test.completed` | required `result`: `passed`, `failed`, `cancelled`; optional nonnegative `duration_ms`, `tests_run`, `tests_failed` (`tests_failed` cannot exceed `tests_run`) |
| `build.completed` | required `result`: `passed`, `failed`, `cancelled`; optional nonnegative `duration_ms` |
| `session.started`, `session.ended` | `{}` |

`source` is one of `skill`, `cli`, `mcp`, `github`, or `ci`.

Metadata must not contain source code, file contents, diffs, prompts, command
text, environment values, credentials, or secrets. In addition to per-event
allowlists, the API rejects forbidden field names and recognizable secret
patterns. Do not place free-form source text in fields intended for structured
activity.

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

Startup applies the additive `trac/migrations/001_project_scoped_events.sql`
migration once, tracked in `schema_migrations`. It adds public actor IDs,
projects, memberships, device project/scopes, event project references, and the
durable stream sequence without deleting existing rows. Existing users receive
a deterministic personal project and existing devices are assigned to it.
Legacy feed rows are associated only when an actor can be matched to an
existing user; unmatched historical rows remain unscoped and are not exposed
through authenticated project feeds.
