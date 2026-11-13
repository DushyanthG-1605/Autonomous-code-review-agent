# TRAC Implementation Plan

This plan describes a dependency-ordered path from the audited repository to the next AI-native engineering intelligence release. It is a proposal for approval, not authorization to begin implementation. Keep the current FastAPI, SQLite, collector, and dashboard during the early phases; avoid a framework rewrite.

## Phase 1 — Canonical project, identity, and event contract

**Depends on:** Architecture approval; none of the later phases.

- Define project/workspace ownership, user identity, device/agent identity, task identity, event ID/idempotency scope, timestamps, and event-versioning rules.
- Align the accepted event types and field allowlists across `trac/models.py`, the collector CLI, skill instructions, review reporter, and dashboard. Decide whether progress and test events are new event types or versioned variants of existing events.
- Establish explicit data classification, maximum lengths, privacy allowlists, and event actor attribution.
- Publish representative request/response examples and compatibility rules before changing ingestion.

**Exit gate:** Contract reviewed; producers and consumers map to one versioned schema; unknown versions/types and invalid fields have explicit behavior.

## Phase 2 — Authorization, tenant boundaries, and privacy controls

**Depends on:** Phase 1 identity/project model.

- Scope devices and all event reads/writes/streams to an authorized project. Define administrative bootstrap and revocation without relying on public endpoints.
- Protect or retire the legacy `/api/events` paths; preserve review reporting through an authenticated compatibility path during transition.
- Enforce server-side allowlists and value bounds, review secret redaction, constrain query limits, enable SQLite foreign-key enforcement, and make throttling behavior explicit for multi-worker use.
- Define retention, export/inspection, consent/pause/revoke behavior, and auditability.

**Migration strategy:** Additive schema changes first; backfill project ownership only where an explicit mapping exists. Quarantine ambiguous historical rows rather than assigning a guessed owner. Keep legacy routes behind the same authorization layer before removing callers.

**Exit gate:** Tests prove cross-project isolation, revoked-device denial, privacy rejection/redaction, and bounded request/query behavior.

## Phase 3 — Collector, skill, and agent connection lifecycle

**Depends on:** Phases 1 and 2.

- Add secure pairing-code issuance/claiming (short-lived, single-use, hashed at rest) and a server-owned connection/device lifecycle.
- Make consent explicit and ensure selected categories govern collection and server authorization.
- Keep local queue/retry/pause behavior; ensure statuses expose failed auth, malformed events, and rejected records without leaking secrets.
- Implement agent-specific adapters only at the collector boundary; they should emit the canonical contract rather than inventing event variants.

**Migration strategy:** Existing token login remains temporarily supported for local development and existing collectors. Provide a documented token-to-pairing transition and a rollback window. Do not silently rotate or invalidate existing device tokens.

**Exit gate:** End-to-end tests cover pairing expiry/replay, user cancellation, revoke/pause, reconnect, offline queueing, and approved field collection.

## Phase 4 — Durable event persistence and compatibility mapping

**Depends on:** Phases 1 and 2; collector integrations in Phase 3 can use this once stable.

- Introduce versioned database migrations and normalized project/user/device/task/event relationships with explicit indexes and retention strategy.
- Use owner/project-scoped idempotency keys and transactions covering persistence plus any outbox record.
- Preserve the existing review `events` contract through an adapter while moving new producers to the canonical event store.
- Decide whether SQLite remains sufficient for the MVP; change the database only when concurrency/availability requirements justify it.

**Migration strategy:** Create additive tables/columns and indices; backfill only unambiguous records; dual-write or adapter-write for a measured transition period; compare counts and IDs; switch reads behind a reversible feature/config gate; retain a verified backup before any destructive cleanup.

**Exit gate:** Migration runs on a copy of existing data, is repeatable or safely fails on replay, and compatibility tests verify old and new writers/readers.

## Phase 5 — Authenticated real-time delivery

**Depends on:** Phase 2 authorization and Phase 4 durable event persistence.

- Publish events only after the database transaction commits, preferably via a transactional outbox.
- Implement authenticated/project-scoped SSE with event IDs, reconnect cursors, heartbeats, disconnect handling, bounded buffering, and replay/retention semantics.
- Keep polling as a temporary fallback until SSE delivery and reconnect behavior are measured.

**Migration strategy:** Retain the current heartbeat endpoint as a compatibility response only until clients move. Introduce the real stream under tested authorization; do not broadcast from process-local in-memory state when deployment may use multiple workers.

**Exit gate:** Integration tests prove newly accepted events arrive once in-order within the stated bounds, reconnect replays safely, cross-project subscriptions are denied, and disconnected clients release resources.

## Phase 6 — Dashboard projection and removal of demo ambiguity

**Depends on:** Phase 1 contract, Phase 4 read model, and Phase 5 live delivery; authorization from Phase 2 is required before displaying real multi-user data.

- Keep the current logo, favicon, layout, CSS/theme, and responsive behavior; update data projection rather than replacing the frontend stack.
- Replace hardcoded counts/statuses and API-failure demo records with explicit loading, empty, offline, and development-only fixture modes.
- Derive active people/tasks, blockers, completion, charts, current work, and recent activity from canonical task/event state, avoiding event counts that masquerade as unique task counts.
- Render unknown event types safely, show sanitized event details, add member/type/agent/time filters, and support pause-feed as UI rendering pause only.

**Migration strategy:** Keep one dashboard client against a compatibility API while read models are introduced. Compare new projections to fixture and production-like data; switch one panel at a time. Never mix seed fixtures with production records.

**Exit gate:** Browser smoke checks cover Overview, Live Activity, Integrate, Connections, theme persistence, loading/empty/offline states, unknown event types, and live updates without reload.

## Phase 7 — GitHub/CI sources, rollout, and legacy retirement

**Depends on:** Phases 1–6, especially authorization, durable persistence, and event delivery.

- Add GitHub App/webhook and CI adapters with least-privilege credentials, signature verification, deduplication, and source-specific consent/permissions.
- Preserve the current PR review agent as a distinct source; do not conflate AI review scoring with work progress.
- Add operational diagnostics, migrations/runbook, backup/restore validation, rate/retention metrics, and integration contract tests.
- Remove compatibility routes, legacy schema, and fallback paths only after usage evidence confirms no remaining producers or readers.

**Migration strategy:** Roll out adapters behind configuration flags; validate signatures and event identity in a non-mutating mode; enable one source at a time; compare received/stored/deduplicated counts; maintain rollback switches and the old API during a defined deprecation interval.

**Exit gate:** Source integrations are idempotent and authorized; rollback and data recovery have been tested; no production view depends on demo records or unauthenticated routes.

## Cross-phase quality gates

- Keep the existing Python, Vitest, and collector end-to-end suites green; add tests at contract and authorization boundaries before changing producers.
- Use additive schema migration and explicit historical-data mapping; preserve review events and collector queues during transition.
- Do not add productivity scoring, attendance tracking, prompt/source capture, or hidden collection.
- No production deployment until the unauthenticated legacy paths and project scoping are resolved.

## Approval boundary

No phases in this plan have started. The architecture audit fixed only the confirmed dashboard render crash and stream-route precedence issue, added a route-order regression test, and recorded the existing baseline. Await approval before implementing the planned platform changes.
