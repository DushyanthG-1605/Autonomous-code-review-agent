# Universal TRAC Skill Integration Contract

This is the planned shared contract for future TRAC Skill integrations. It is documentation only; it does not activate collection or authorization behavior.

## Event envelope

Every event uses the same envelope regardless of host IDE or agent:

```json
{
  "event_id": "evt_01...",
  "project_id": "project_01...",
  "actor": { "user_id": "user_01...", "device_id": "device_01..." },
  "event_type": "task.progress",
  "timestamp": "2026-11-13T12:00:00Z",
  "metadata": {}
}
```

`metadata` is allowlisted per event type. Source code, file contents, prompts, credentials, and secrets are never part of the contract. Authorization, inspection, pause, and revocation must be enforced by the collector and backend before this contract is considered implemented.

## Event types

The shared lifecycle includes `task.started`, `task.progress`, `task.blocked`, `task.completed`, `session.started`, `session.ended`, `git.commit`, and `test.completed`.

IDE-specific adapters may only handle installation, configuration, or optional event hooks. They must emit the same envelope, permissions, lifecycle, and privacy semantics into a universal collector.
