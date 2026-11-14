# TRAC Architecture Audit

**Audit snapshot:** 2026-11-13  
**Scope:** Current checkout, including the uncommitted TRAC collector/dashboard changes present during the audit.  
**Status:** Phase 1 audit snapshot with the current Phase 2–4 implementation recorded below.

> **Scope note:** Sections through “Remaining risks and blockers” preserve the
> Phase 1 audit snapshot and findings as they stood then. The current Phase 2–4
> architecture and superseding behavior are recorded in the update below.

## Executive summary

TRAC currently combines two applications:

1. A LangGraph autonomous GitHub pull-request review CLI using GitHub, Groq, repository cloning, retrieval, and static analysis.
2. A FastAPI/SQLite activity service with a static HTML/CSS/JavaScript dashboard and a local `trac` event collector.

The versioned ingestion route has bearer-device authentication, schema allowlists, size and batch limits, duplicate suppression, and collector-side privacy filtering. The legacy dashboard event API remains unauthenticated and is shared by the review agent. User/device records exist, but there are no project/team entities or project-scoped authorization rules.

The browser does not consume SSE. The stream endpoint only emits initial/periodic heartbeat frames, and prior to this audit its route was shadowed by the integer event-detail route. The endpoint was moved before the dynamic route; a regression test now protects that route order. A dashboard refresh failure was also confirmed: the renderer called retired agent/repository/people DOM mounts. That obsolete call was removed.

## Runtime shape

```text
Browser ── Vite proxy (/api) ── FastAPI ── SQLite
   │                               ▲
   └── static dashboard             │ POST /v1/events (Bearer device token)
                                   │
Local trac CLI ── local queue ─────┘
       ├── explicit task/session events
       ├── Git hooks
       └── optional Claude Code hooks

Review CLI ── LangGraph ── GitHub API / Groq API
     └── tools.trac_reporter ── unauthenticated POST /api/events
```

The browser entry point is [dashboard.html](../trac/static/dashboard.html), served by FastAPI at `/` and by Vite from its static root in development. [vite.config.mjs](../vite.config.mjs) proxies `/api` to `127.0.0.1:8000`; the package `dev` script starts Vite and Uvicorn concurrently. Logo, symbol, favicon, theme, and responsive CSS assets are present under `trac/static/`.

## API inventory

| Method and path | Request | Response / behavior | Authentication |
|---|---|---|---|
| `GET /` | None | Serves the dashboard HTML | None |
| `GET /legacy` | None | Serves the same current dashboard HTML; it is not a preserved legacy dashboard | None |
| `POST /api/events` | `EventCreate`: required `type`, `source`, `summary`; optional `repo`, `ref`, `actor`, `execution_id`, `status`; `metadata` defaults to `{}` | An `Event` including DB `id` and `timestamp`; upserts on non-null `execution_id`, otherwise inserts | None |
| `GET /api/events` | Optional `source`; integer `limit` defaults to 100 | Newest-first list of `Event` records. `limit` has no explicit bounds | None |
| `GET /api/events/{event_id}` | Integer path ID | One `Event`, or HTTP 404 | None |
| `POST /v1/events` | `{"events": [...]}`; each event has a version-1 envelope (`event_id`, `schema_version`, `type`, `ts`, optional `session_id`, `repo`, `branch`, `source`, `tool`, `data`) | HTTP 202 and `{"accepted": n, "duplicates": n, "rejected": [{"event_id": ..., "reason": ...}]}` | Bearer device token |
| `GET /api/events/stream` | None | `text/event-stream`: one `ping` frame followed by `heartbeat` frames every 15 seconds. It does not publish activity records | None |
| `/static/*` | Asset path | FastAPI static-file mount | None |

Versioned ingestion rejects missing/invalid bearer tokens with 401, malformed JSON or payloads with 400/422, batches above 100 and bodies above 256 KiB with 413, and per-device over-limit traffic with 429 plus `Retry-After`. It reports per-item validation/privacy rejection in the 202 response. The accepted event `type` is constrained by `EventType`; extra envelope/data properties are ignored. The API does not accept a structured `user` or `project` object; the authenticated device determines the persisted `user_id`.

The legacy `EventCreate` endpoint is a separate shape and persistence path from `/v1/events`. Its client-supplied actor, source, status, and arbitrary metadata are stored as given. The review agent uses this legacy endpoint.

## Event types and persistence

The version-1 model in [models.py](../trac/models.py) currently accepts:

- Sessions: `session.start`, `session.end`
- Tasks: `task.started`, `task.completed`, `task.blocked`
- Tool activity: `file.edited`, `command.run`, `agent.prompt_submitted`
- Git: `git.commit`, `git.push`, `git.branch`

Per-type Pydantic data models keep named fields and ignore unknown fields. Notably, the current server does **not** accept the proposed `task.progress`, `session.started`/`session.ended`, or standalone `test.started`/`test.passed`/`test.failed` events. Tests/builds are represented as `command.run` with a `kind` and `exit_code`.

`/v1/events` writes to `activity_events` using `event_id` as a global primary key. Replayed IDs are ignored; accepted task/Git/session records and test-kind command records are also mirrored into `events` for the existing dashboard feed. The mirror uses the device owner's display name/login rather than trusting an actor in the incoming envelope. Other event kinds remain only in `activity_events`.

The legacy `/api/events` writes directly to `events`. A non-null `execution_id` upserts the existing row; without it, each POST inserts another row.

## Database inventory and migrations

SQLite is initialized by [db.py](../trac/db.py), called at FastAPI startup and by the management CLI. Tables:

| Table | Purpose and relationships |
|---|---|
| `events` | Legacy dashboard/review activity: `id`, type/source/summary, repo/ref/actor, timestamp, execution ID, status, JSON metadata. Partial unique index on non-null `execution_id`. |
| `users` | `id`, unique `github_login`, display name, creation time. No organization/team or project relationship. |
| `devices` | Device token hash, label, creation/revocation timestamps, `user_id` foreign-key declaration. |
| `activity_events` | Versioned event envelope and sanitized event data; `event_id` primary key and `user_id` foreign-key declaration. Indexed by user/time and repo/time. |

There is no migration framework or schema-version table. Initialization creates missing tables and applies a few conditional `ALTER TABLE` statements to `events`; newer tables are created with `IF NOT EXISTS`. SQLite foreign-key enforcement is not enabled in `get_connection()`, so declared constraints are not currently enforced by the connection. There is no `projects` table, `project_id` column, tenant key, or task aggregate table.

## Authentication and authorization

- `python -m trac.manage create-user <github_login>` creates a user and device token; it prints the raw token once and stores its SHA-256 hash.
- `/v1/events` checks the bearer token hash and rejects revoked devices. This is device authentication, not an interactive user login or project authorization system.
- `revoke-device` sets a device revocation timestamp.
- `/api/events` reads, writes, and the SSE endpoint do not authenticate or scope requests. This is a material privacy/security gap if the service is reachable beyond a trusted local network. The development Uvicorn command binds to loopback, but that is not a production authorization boundary.
- Rate limiting for `/v1/events` is an in-memory process-local map; it is not shared across workers or restarts.

## Existing integrations

### GitHub code review

The original review path remains in [app/main.py](../app/main.py), [agent/graph.py](../agent/graph.py), and [tools/github_tools.py](../tools/github_tools.py). It obtains a `GITHUB_TOKEN` from the process environment/ignored `.env`, reads PR files and author, clones/retrieves repository context, runs Ruff and secret scanning, calls Groq for review findings, and can publish PR reviews through PyGithub. The token is not hardcoded in tracked source. The root `.env` is ignored; its values were not inspected.

This is GitHub PR-review integration, not a GitHub App/webhook integration for general engineering activity. The review workflow reports events through [tools/trac_reporter.py](../tools/trac_reporter.py) to the unauthenticated legacy endpoint. No CI pipeline event integration was found.

### Collector, skill, and MCP

- [collector/trac_collector/cli.py](../collector/trac_collector/cli.py) is installed as the `trac` executable through [collector/pyproject.toml](../collector/pyproject.toml). Commands include `login`, `emit`, `flush`, `status`, `pause`, `resume`, `data`, `logout`, `init`, `uninit`, and `hook claude`.
- The collector keeps a local queue, retries batches, supports offline queuing, redacts selected secret-like values, and sends authenticated batches to `/v1/events`.
- Git hooks capture commit statistics, push metadata, and branch changes. Optional Claude Code hooks map session/prompt-submission/file-edit/command outcomes to bounded metadata; hook payload contents are not serialized into the event.
- The current consent-first lifecycle is documented in [skill/trac/SKILL.md](../skill/trac/SKILL.md).
- No MCP server implementation or MCP configuration was found in the repository.

## Dashboard implementation, live behavior, and mock values

The dashboard is a single static HTML file with inline JavaScript plus a stylesheet; it is not a component-based frontend. It preserves TRAC assets, cyan/neutral dark and light theme tokens, responsive layout, and local theme preference.

- It fetches `GET /api/events?limit=200` and polls every five seconds. It does not open an `EventSource` or WebSocket.
- The stream route sends only `ping`/`heartbeat`; it does not query or broadcast database events.
- Mock events are generated in the dashboard only after an API fetch/response failure. They are not gated to development mode and can be displayed as if they were real activity while the connection indicator shows reconnecting.
- KPI rendering also substitutes fixed fallback values for active/task/blocked/completed counts; notably those fallbacks apply to zero data as well as demo data.
- The two Connections cards are hardcoded sample identities/statuses. They do not reflect `devices` or a live connection registry.
- The displayed work chart still calculates commit/PR/finding series; the current recent-activity renderer still selects review events; and the working-now renderer retains contributor/code-review fields. These view labels and calculations are not yet a coherent live-work projection.
- Event details expose legacy review metadata, not an exact sanitized version-1 event envelope. The dashboard event query reads the mirror table, not `activity_events` directly.
- The `GET /legacy` route serves this same dashboard and does not provide rollback to the earlier UI.

## Working, incomplete, and absent

**Working and covered:** local token creation/revocation, authenticated versioned ingestion, event-ID replay deduplication, bounded payloads/rate limit, event mirroring, collector offline queue/flush, selected Claude/Git hook metadata collection, and the older GitHub PR review/reporting flow.

**Incomplete or inconsistent:** unauthenticated legacy API, no project/team authorization, no project relationship in activity storage, process-local throttling, no database migration versioning, no actual event broadcast, no EventSource client, no server-generated pairing flow, hardcoded connection cards and dashboard fallback metrics, and mismatch between the collector's version-1 schema and the proposed `task.progress`/test event contract.

**Not found:** MCP implementation, CI activity integration, GitHub activity webhook/App integration, durable SSE replay/cursors, and a production deployment configuration.

## Audit fixes

1. `GET /api/events/stream` was declared after `GET /api/events/{event_id}`. The dynamic route matched `"stream"` first and FastAPI's integer validation prevented the stream route from being reached. The stream route is now registered before the detail route, with a route-order regression test. The stream still intentionally sends heartbeat frames only.
2. Dashboard refresh/render invoked `renderEntities()`, which wrote into agent/repository/people mounts removed from the current navigation. A successful response therefore caused a JavaScript null dereference. The obsolete render call was removed; its data views are not part of the current UI.

No new product integrations or event contracts were implemented in this audit.

## Baseline checks

| Check | Result |
|---|---|
| `.\venv\Scripts\python.exe -m pytest -q` | 28 passed (includes the stream-route regression test). FastAPI/pytest deprecation warnings remain. |
| `.\venv\Scripts\python.exe -m pytest -q collector\tests` | 16 passed. |
| `pnpm test` | 2 Vitest tests passed (theme-token tests). |
| `.\venv\Scripts\python.exe scripts\e2e_activity.py` | Passed: isolated backend/collector flow, 11 unique events, 11 replay duplicates, pause/offline queue/resume, and persisted-data privacy sentinels. |

The end-to-end script starts the backend and verifies the collector/API path. It does not exercise browser rendering or actual SSE event delivery.

## Remaining risks and blockers

1. **High — public legacy event API:** unauthenticated event reads and writes can expose or alter activity if the service is network-accessible. Establish authorization and tenant/project scoping before external deployment.
2. **High — privacy claims exceed enforcement:** collector redaction is one layer; direct API callers can submit arbitrary legacy metadata and allowed versioned strings may carry sensitive content. Server-side field/type/length constraints and inspection/retention policy need to be designed and tested.
3. **High — no project boundary:** user/device records are not tied to projects; `repo` is an unverified string, not an authorization relationship.
4. **Medium — false demo activity:** mock fallback records and fixed count defaults can make empty/offline state look like real work.
5. **Medium — live stream is not live:** heartbeat-only SSE and 5-second browser polling are independent; no event fan-out or replay semantics exist.
6. **Medium — schema migration and event identity:** schema changes are ad hoc, and event-ID uniqueness is global rather than scoped to an owner/project.
7. **Medium — integration gaps:** pairing is not implemented, CI/GitHub activity hooks are absent, and the current event type schema does not match the desired progress/test lifecycle.

## Current architecture after Phases 2–4

The Phase 1 findings above are historical. The current runtime still uses
FastAPI, SQLite, the static dashboard, and the existing collector, but project
scoping, canonical event ingestion, task management, and real SSE delivery have
since been added. Phase 4 adds one-time device pairing, the universal
consent-based skill, and a collector that reports structured task events.

### Current API surface

| Method and path | Behavior |
|---|---|
| `POST /v1/events` | Authenticated version-1 compatibility and strict version-2 event ingestion; task lifecycle version-2 events require an existing authorized `task_id`. |
| `GET /v1/events`, `GET /v1/events/{event_id}` | Authenticated project event list/detail; project heads see all, members see their own events and activity on their assigned tasks. |
| `GET /v1/events/stream`, `GET /api/events/stream` | Authenticated durable SSE replay with event IDs, `Last-Event-ID`, heartbeat, reconnect support, and the same project/member visibility. |
| `POST /api/events`, `GET /api/events`, `GET /api/events/{id}` | Authenticated compatibility API used by the review reporter/dashboard. |
| `GET/POST /v1/projects`, `GET /v1/projects/{id}` | Membership project listing, project creation, and project detail. Creation returns a one-time project-scoped device token. |
| `GET/POST /v1/projects/{id}/tasks` | Head/member-scoped task listing and head-only task creation/assignment. |
| `GET/PATCH /v1/tasks/{id}`, `GET /v1/me/tasks` | Authorized task detail/history, updates, and assigned-work listing. |
| `GET /v1/projects/{id}/activity`, `GET /v1/projects/{id}/overview` | Project-head-only activity and task-count overview. |
| `POST /v1/dev/login` | Optional username-only development login; disabled unless explicitly enabled for development and rejected in production. |
| `POST /v1/pairings`, `POST /v1/pairings/claim` | Authenticated, scoped code issuance and rate-limited one-time device pairing. |
| `GET/DELETE /v1/me/device` | Inspect current device identity/scopes or revoke the device. |

### Current data and event flow

`schema_migrations` tracks three additive SQLite migrations:

1. `001_project_scoped_events.sql` adds public user IDs, projects,
   memberships, device project/scopes, activity project/task references, and
   the durable event-stream sequence. Historical rows are assigned only where
   the migration can establish an unambiguous owner/project.
2. `002_projects_tasks_identity.sql` adds optional email/GitHub username and
   user role, project key/description/head, device auth mode, `tasks`, and
   `task_updates`, with foreign keys and indexes.
3. `003_device_pairing.sql` adds hashed one-time pairing records and persistent
   rate-limit buckets; it preserves existing users, projects, devices, and
   activity.

The main persisted tables are `users`, `projects`, `project_memberships`,
`devices`, `tasks`, `task_updates`, `activity_events`,
`activity_event_stream`, and the retained compatibility `events` table.
SQLite foreign-key checks are enabled per connection. Device tokens are
random and stored only as SHA-256 hashes.

For version-2 task events, the authenticated credential supplies actor and
project; body identity claims are ignored. The task must belong to that
credential's project, and the actor must be the project head or task assignee.
Event persistence, task state change, task-update history, compatibility
mirror, and stream sequence insertion are transactional. Duplicate event IDs
do not create a second task update. Agent/event progress is stored with
`progress_is_estimate=true`; commits and elapsed time do not infer task
percentages. Direct task API reports are marked non-estimated.

The SSE implementation polls `activity_event_stream` and joins persisted
events, so event replay survives process restarts and does not rely on an
in-memory-only bus. The dashboard now uses authenticated fetch-streaming SSE
and no longer substitutes mock events on API failure. SQLite/shared-file
multi-process behavior is supported locally; multi-host streaming still needs
shared transactional storage or an external broker.

### Current authorization boundaries and gaps

- Project heads see all tasks and authorized project activity. Members see
  only their assigned tasks, their own activity, and activity attached to
  assigned tasks. Cross-project task/event details are hidden with `404`.
- Project-specific API access requires a credential scoped to that project.
- The local development login is passwordless and suitable only for a trusted
  development environment. Production OAuth/SSO login is not implemented.
- Version-1 collector events remain compatible but have no structured task
  UUID, so legacy task events cannot be linked to `tasks`; new task lifecycle
  producers must use version 2 with a real task ID.
- Pairing requires an existing project-scoped device credential with
  `events:ingest`; production OAuth/SSO and self-service enrollment are not
  implemented.
- GitHub App/webhook and CI activity integrations and multi-host event delivery
  remain unimplemented.
- Project dashboard projections are still limited; task APIs are available,
  but the dashboard has not been redesigned into a complete task-management
  interface.

### Universal skill and collector

- [`skill/trac/SKILL.md`](../skill/trac/SKILL.md) requires explicit consent,
  checks assigned tasks, reports meaningful lifecycle milestones, and forbids
  source/prompt/secret collection.
- [`collector/trac_collector/cli.py`](../collector/trac_collector/cli.py)
  provides pairing login, task lifecycle commands, a bounded offline queue,
  redaction, pause, inspection, and server-side device revocation.
- The collector stores device credentials in the OS keyring when available;
  POSIX restricted-file fallback is used only when the keyring package is
  unavailable. A configured keyring failure is surfaced.
- Host skill locations and documented compatibility are in
  [`TRAC_UNIVERSAL_INTEGRATION.md`](TRAC_UNIVERSAL_INTEGRATION.md). They share
  one CLI and backend; automatic hooks are not assumed and Git hooks require
  an explicit opt-in.
- Offline queues are local to the user profile. The collector retries on later
  flush attempts with backoff; there is no background daemon or multi-machine
  queue synchronization.
