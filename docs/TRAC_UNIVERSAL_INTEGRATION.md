# TRAC Universal Skill and Collector

Phase 4 provides one consent-based skill and one collector for compatible
coding agents. Host-specific behavior is limited to installing the skill and
optional local hooks; task lifecycle events use the same authenticated
version-2 `/v1/events` API.

## Installation and explicit activation

Install the collector from a TRAC checkout:

```text
python -m pip install -e collector
trac init --target <environment>
```

`trac init` installs `skill/trac/SKILL.md` into the host's documented project
skill directory. It does not install hooks or activate tracking. Supported
targets are `claude`, `cursor`, `codex`, and `copilot`; `generic` installs to
`.agents/skills/trac`. `auto` selects a detected host only if exactly one
supported host directory is present; otherwise select `--target` explicitly.
An existing different skill file is never overwritten.

Before any tracking, the agent must ask the developer for explicit consent.
After consent, pair a device with an authorized project. A project member or
head with `events:ingest` can create a scoped one-time pairing:

```http
POST /v1/pairings
Authorization: Bearer <existing-project-device-credential>
Content-Type: application/json

{
  "project_id": "018f5c2a-7b6d-7a22-b33a-cb1d5e12f042",
  "scopes": ["events:read", "events:ingest"]
}
```

The server returns a one-time `pairing_code`, an `expires_at` timestamp ten
minutes in the future, and the granted scopes. The code is stored only as a
hash, can be claimed once, is rate-limited, and is invalidated if its issuing
device is revoked. Do not put the code or long-lived credentials in chat,
source files, shell history, or logs. Claim it with:

```text
trac login --url https://<trac-server>
```

The collector prompts without echoing the code. For automation, it accepts the
code through standard input without echoing it:

```text
trac login --url https://<trac-server> --pairing-code-stdin
```

The resulting device is bound to the pairing creator's user, project, and
scopes. Device tokens are stored in the operating-system credential store
(Windows Credential Manager, macOS Keychain, or a configured Linux keyring).
On POSIX systems only, a restrictive-permission local file is used if the
keyring package is unavailable; a configured but failing keyring causes login
to fail rather than silently writing credentials in plaintext. Install with
the declared dependencies to use the OS credential store.

## Lifecycle commands and data controls

```text
trac status
trac tasks
trac task start <task-uuid>
trac task progress <task-uuid> --percent 50 --note "Core flow implemented"
trac task block <task-uuid> --reason-code dependency --note "Waiting on API access"
trac task done <task-uuid> --note "Relevant checks passed"
trac pause
trac resume
trac data
trac disconnect
```

Task IDs are resolved against the authenticated developer's assigned work.
`trac tasks` caches only a bounded list of assigned UUIDs so already known task
events can be queued offline; the server rechecks task membership and actor
authorization when they are delivered. If the task has not been seen while
online, the collector will not queue it offline.

Event IDs are UUIDs and are generated once before queueing, making retries
idempotent. The queue is capped at 1,000 events and 2 MiB; oldest entries are
dropped when the cap is reached and counted in `trac status`. Retries use
exponential backoff, capped at five minutes. Events are retried on the next
collector flush attempt; the collector does not run a background daemon.
`trac pause` prevents new event collection and sends. `trac data` displays
queued and recently sent, redacted records. `trac disconnect` revokes the
current device at the server and clears pending events; `trac logout` only
removes local credentials and does not revoke the server device.

Optional Git hooks must be explicitly installed with `trac init --git-hooks`.
They report limited commit/branch metadata through the compatibility collector
path. Claude Code hook installation is not automatic. Hooks are not required
for task reporting and must not be enabled without the developer's consent.

## Host compatibility

The CLI uses one workflow and does not depend on host-specific event APIs.
Each host is supported for skill discovery only when it documents the project
skill directory below; no automatic hook behavior is assumed.

| Environment | Skill location | Compatibility status |
| --- | --- | --- |
| Claude Code | `.claude/skills/trac/SKILL.md` | Documented project skill mechanism; install via `trac init --target claude`. |
| Cursor | `.cursor/skills/trac/SKILL.md` | Documented project skill mechanism; install via `trac init --target cursor`. |
| OpenAI Codex | `.agents/skills/trac/SKILL.md` | Documented project skill mechanism; install via `trac init --target codex`. |
| VS Code / GitHub Copilot | `.github/skills/trac/SKILL.md` | Documented project skill mechanism; install via `trac init --target copilot`. Codex skill discovery in VS Code is experimental and is not required by this path. |
| Other agents | Agent's documented project skill directory | Compatible only if the agent can execute the `trac` CLI and follow the installed `SKILL.md`; configure manually and verify skill discovery. No automatic support is claimed. |

These are documented discovery mechanisms, not claims that every supported
agent/runtime has been exercised end to end. Users should confirm the skill is
visible to their chosen agent before enabling tracking.

The project-skill locations above were checked against the host documentation:
[Claude Code skills](https://code.claude.com/docs/en/skills),
[Cursor Agent Skills](https://cursor.com/docs/skills),
[OpenAI Codex/Agent Skills](https://learn.chatgpt.com/docs/build-skills), and
[VS Code Agent Skills](https://code.visualstudio.com/docs/agent-customization/agent-skills).

## API contract

Pairing routes and device revocation are documented in
[`TRAC_EVENT_CONTRACT.md`](TRAC_EVENT_CONTRACT.md). Version-2 event fields and
metadata allowlists are defined there; task events include a real authorized
`task_id` and the backend derives actor and project from the device credential.
The host name never changes the event envelope or API.

## Limitations

- Production OAuth/SSO and self-service project creation are not implemented;
  an authorized member or head must already have a project-scoped credential
  to issue a pairing code.
- Linux secure storage depends on a working Secret Service/KWallet/keyring
  backend. Login fails if an installed keyring cannot access its backend.
- The offline queue is local to one user profile and is not synchronized across
  machines.
- Optional legacy hooks can produce version-1 compatibility events. Structured
  task lifecycle events from the skill and CLI use version 2.
