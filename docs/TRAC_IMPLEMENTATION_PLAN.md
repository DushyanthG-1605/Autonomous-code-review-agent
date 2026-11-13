# TRAC Implementation Plan

This dependency-ordered roadmap reflects work completed through Phase 3. It
preserves FastAPI, SQLite, the collector, and the dashboard; no framework
rewrite is planned. Phases 4–7 are future work and require approval before
implementation.

## Phase 1 — Architecture audit and stabilization (complete)

- Inventory existing routes, storage, authentication, integrations, dashboard,
  and tests.
- Record the baseline architecture and fix only confirmed run-blocking defects.
- Preserve existing dashboard branding and working review behavior.

**Evidence:** `docs/TRAC_ARCHITECTURE.md`; baseline Python, JavaScript, and
collector/API checks.

## Phase 2 — Universal event ingestion and real-time streaming (complete)

- Define strict version-2 event and metadata allowlists; preserve version-1
  collector and review-reporter compatibility.
- Require project-scoped, hashed device credentials and enforce event read and
  ingest scopes.
- Add idempotent persistence and durable SQLite event-stream replay with SSE
  IDs, reconnect cursors, heartbeats, and authenticated access.
- Remove dashboard mock-event fallback and use authenticated streaming.

**Migration:** Additive migration `001_project_scoped_events.sql`; preserve
existing rows and backfill only unambiguous ownership.

**Evidence:** `docs/TRAC_EVENT_CONTRACT.md`; ingestion, auth, privacy,
idempotency, SSE, and collector E2E tests.

## Phase 3 — Projects, tasks, and developer identity (complete)

- Add project descriptions/keys/heads, optional email and GitHub username,
  users' global role, task status/priority/progress, assignment, and task update
  history.
- Add project/task/me/activity/overview APIs with project-head and
  assigned-member authorization.
- Link version-2 task events to real tasks and atomically update task state and
  history. Label event-reported percentages as estimates; never infer exact
  progress from elapsed time or commit count.
- Add opt-in local development login that is rejected in production.

**Migration:** Additive migration `002_projects_tasks_identity.sql`; backfill
project keys from slugs, project heads from owner memberships, and GitHub
usernames from existing logins. Do not delete historical activity.

**Evidence:** `docs/TRAC_PROJECTS_TASKS.md`; project/task authorization,
event-linking, estimate, and migration tests. Version-1 task events remain
unlinked because that envelope has no task UUID.

## Phase 4 — Producer alignment and connection lifecycle

**Depends on:** Phases 2 and 3.

- Align collector, skill, CLI, MCP adapters, and review reporter with the
  version-2 contract where structured task IDs are available.
- Introduce secure, short-lived, single-use device pairing only if deployment
  needs a user-friendly alternative to project-scoped token bootstrap.
- Preserve local queue/retry/pause behavior; expose authorization and
  validation failures without leaking secrets.
- Make consent explicit for collection categories and ensure the collector
  sends only allowlisted metadata.

**Migration:** Keep version-1 ingestion and management-issued tokens during
transition. Roll out producer updates by source, compare accepted/rejected and
duplicate counts, and retain a rollback path.

**Exit gate:** Integration checks cover task-ID validation, consent/pause,
offline retries, credential revoke, pairing replay/expiry if implemented, and
privacy sentinels.

## Phase 5 — Storage and operational hardening

**Depends on:** Phases 2–4.

- Define event/task retention, export, backup/restore, and migration runbooks.
- Measure SQLite write contention and stream query behavior under expected
  single-host load; select shared storage or a broker only if deployment needs
  multi-host delivery.
- If an external publisher is added, introduce a transactional outbox so
  database commit and publish intent cannot diverge.
- Make rate limiting and operational diagnostics appropriate for the selected
  deployment topology.

**Migration:** Add schema changes only through additive, versioned migrations;
test upgrades against a copy of existing user data and prove rollback/restore
before destructive cleanup.

**Exit gate:** Backup restore, migration replay, bounded query/stream behavior,
and documented capacity/availability limits are verified.

## Phase 6 — Dashboard projection and task workflows

**Depends on:** Phases 2, 3, and 5.

- Add task/project views and derive overview metrics from authorized task and
  event records, not fixed counts or event counts presented as unique tasks.
- Keep the TRAC logo, favicon, cyan accent, theme, sidebar, and existing
  dashboard behavior.
- Provide clear loading, empty, offline, and unauthorized states; never render
  synthetic events as real activity.
- Safely render task histories and progress estimates with project/member
  filters.

**Migration:** Keep the existing authenticated event API and stream while
introducing task projections incrementally. Verify member and head visibility
with test accounts before displaying multi-user activity.

**Exit gate:** Browser checks cover project/task workflows, live updates,
permissions, theme persistence, loading/empty/offline states, and no mock data
in production mode.

## Phase 7 — GitHub/CI adapters, rollout, and compatibility retirement

**Depends on:** Phases 1–6.

- Add GitHub App/webhook and CI adapters with signature verification,
  least-privilege permissions, consent, and source-specific idempotency.
- Keep PR review findings distinct from engineering progress.
- Run source adapters in a non-mutating validation mode, then enable them
  incrementally with received/stored/deduplicated metrics.
- Retire compatibility routes or version-1 payloads only after producer and
  consumer usage confirms they are unused.

**Migration:** Preserve the review reporter contract and event IDs during
rollout. Keep feature/configuration rollback controls and verify data recovery.

**Exit gate:** Integrations are authenticated and replay-safe; rollback and
restore are tested; production views contain no demo records or unauthorized
events.

## Cross-phase quality gates

- Keep root Python, collector, JavaScript, and end-to-end checks green.
- Test authorization, privacy, and idempotency at each new producer/API
  boundary.
- Use additive schema changes and explicit historical mapping; do not guess
  task/user ownership for old events.
- Do not add productivity scoring, attendance tracking, prompt/source capture,
  or hidden collection.
- No production deployment until development login is disabled, every event
  route is authenticated, project scoping is enforced, and deployment limits
  are documented.
